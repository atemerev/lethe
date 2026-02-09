"""Actor runner — executes an actor's LLM loop.

The runner manages the lifecycle of a non-principal actor:
1. Build system prompt from actor config + group awareness
2. Run LLM tool loop until goals are met or max turns reached
3. Handle inter-actor message exchange
4. Auto-notify parent after 1 minute of execution
5. Terminate and report results to parent
"""

import asyncio
import logging
import os
import time
from typing import Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorMessage, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools

# Workspace root — resolved once
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", os.path.expanduser("~/lethe"))

WORKSPACE_CONTEXT = """<workspace>
Your workspace is at: {workspace}
Key paths:
- {workspace}/projects/ — project notes and plans
- {workspace}/skills/ — skill files and docs
- {workspace}/data/ — databases and persistent data
- {workspace}/reference/ — reference materials
Home directory: {home}
Use absolute paths. Never guess paths like /home/user/.
</workspace>"""

logger = logging.getLogger(__name__)

# Notify parent if task takes longer than this (seconds)
PROGRESS_NOTIFY_INTERVAL = 60


class ActorRunner:
    """Runs a non-principal actor's LLM loop asynchronously."""

    def __init__(
        self,
        actor: Actor,
        registry: ActorRegistry,
        llm_factory: Callable,
        available_tools: Optional[Dict] = None,
    ):
        self.actor = actor
        self.registry = registry
        self.llm_factory = llm_factory
        self.available_tools = available_tools or {}

    async def _notify_parent(self, message: str):
        """Send a status notification to the parent actor."""
        actor = self.actor
        if actor.spawned_by:
            parent = self.registry.get(actor.spawned_by)
            if parent and parent.state != ActorState.TERMINATED:
                msg = ActorMessage(
                    sender=actor.id,
                    recipient=actor.spawned_by,
                    content=message,
                )
                try:
                    await parent.send(msg)
                except Exception as e:
                    logger.warning(f"Failed to notify parent: {e}")

    async def run(self) -> str:
        """Run the actor's LLM loop until completion or max turns."""
        actor = self.actor
        start_time = time.monotonic()
        last_notify_time = start_time
        
        try:
            # Create LLM client for this actor
            llm = await self.llm_factory(actor)
            actor._llm = llm
            
            # Register actor-specific tools (send, discover, terminate, spawn, ping, kill, restart)
            actor_tools = create_actor_tools(actor, self.registry)
            for func, _ in actor_tools:
                llm.add_tool(func)
            
            # Register default tools (CLI + file — always available)
            from lethe.actor.integration import SUBAGENT_DEFAULT_TOOLS
            registered_tools = []
            for tool_name in SUBAGENT_DEFAULT_TOOLS:
                if tool_name in self.available_tools:
                    func, schema = self.available_tools[tool_name]
                    llm.add_tool(func, schema)
                    registered_tools.append(tool_name)
            
            # Register additional requested tools
            for tool_name in actor.config.tools:
                if tool_name in SUBAGENT_DEFAULT_TOOLS:
                    continue  # Already registered
                if tool_name in self.available_tools:
                    func, schema = self.available_tools[tool_name]
                    llm.add_tool(func, schema)
                    registered_tools.append(tool_name)
                else:
                    logger.warning(f"Actor {actor.id}: requested tool '{tool_name}' not available")
            
            if registered_tools:
                logger.info(f"Actor {actor.id}: registered tools: {registered_tools}")
            
            # Build initial prompt
            system_prompt = actor.build_system_prompt()
            llm.context.system_prompt = system_prompt
            
            workspace_ctx = WORKSPACE_CONTEXT.format(
                workspace=WORKSPACE_DIR,
                home=os.path.expanduser("~"),
            )
            
            initial_message = (
                f"You are actor '{actor.config.name}'. Your goals:\n\n"
                f"{actor.config.goals}\n\n"
                f"{workspace_ctx}\n\n"
                f"Begin working. Use your tools to accomplish the task. "
                f"When done, call terminate(result) with a detailed summary of what you accomplished.\n"
                f"If something goes wrong, notify your parent with send_message().\n"
                f"If your goals are unclear, use restart_self(new_goals) with a better prompt."
            )
            
            logger.info(f"Actor {actor.id} ({actor.config.name}) starting, tools: {len(llm._tools)}")
            
            response = ""
            for turn in range(actor.config.max_turns):
                actor._turns = turn + 1
                
                if actor.state == ActorState.TERMINATED:
                    break
                
                # Check for incoming messages
                incoming = []
                while not actor._inbox.empty():
                    try:
                        msg = actor._inbox.get_nowait()
                        incoming.append(msg)
                    except asyncio.QueueEmpty:
                        break
                
                # Build the message for this turn
                if turn == 0:
                    message = initial_message
                elif incoming:
                    parts = []
                    for msg in incoming:
                        sender = self.registry.get(msg.sender)
                        sender_name = sender.config.name if sender else msg.sender
                        parts.append(f"[Message from {sender_name}]: {msg.content}")
                    message = "\n".join(parts)
                else:
                    message = "[Continue working on your goals. Call terminate(result) when done.]"
                
                # Check if we should notify parent about long-running task
                elapsed = time.monotonic() - start_time
                if elapsed - (last_notify_time - start_time) > PROGRESS_NOTIFY_INTERVAL:
                    last_notify_time = time.monotonic()
                    await self._notify_parent(
                        f"[PROGRESS] {actor.config.name} still working (turn {turn + 1}/{actor.config.max_turns}, "
                        f"{int(elapsed)}s elapsed). Last: {response[:100] if response else 'starting...'}"
                    )
                
                # Call LLM
                try:
                    response = await llm.chat(message)
                except Exception as e:
                    logger.error(f"Actor {actor.id} LLM error: {e}")
                    await self._notify_parent(f"[ERROR] {actor.config.name} hit an error: {e}")
                    actor.terminate(f"Error: {e}")
                    break
                
                if actor.state == ActorState.TERMINATED:
                    break
                
                # Brief pause to collect any incoming messages
                try:
                    await asyncio.wait_for(actor._inbox.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            
            # Force terminate if max turns reached
            if actor.state != ActorState.TERMINATED:
                elapsed = time.monotonic() - start_time
                result = f"Max turns reached ({actor.config.max_turns} turns, {int(elapsed)}s). Last: {response[:200] if response else 'none'}"
                logger.warning(f"Actor {actor.id} hit max turns")
                await self._notify_parent(f"[MAX TURNS] {actor.config.name}: {result}")
                actor.terminate(result)
            
        except Exception as e:
            logger.error(f"Actor {actor.id} runner error: {e}", exc_info=True)
            await self._notify_parent(f"[FATAL] {actor.config.name} crashed: {e}")
            actor.terminate(f"Runner error: {e}")
        
        return actor._result or "No result"


async def run_actor_in_background(
    actor: Actor,
    registry: ActorRegistry,
    llm_factory: Callable,
    available_tools: Optional[Dict] = None,
) -> asyncio.Task:
    """Start an actor running in the background."""
    runner = ActorRunner(actor, registry, llm_factory, available_tools)
    task = asyncio.create_task(runner.run(), name=f"actor-{actor.id}")
    actor._task = task
    return task
