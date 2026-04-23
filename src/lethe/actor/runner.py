"""Actor runner — executes an actor's LLM loop.

The runner manages the lifecycle of a non-principal actor:
1. Build system prompt from actor config + group awareness
2. Run LLM tool loop until goals are met or max turns reached
3. Handle inter-actor message exchange
4. Auto-notify parent after 2 minutes of execution
5. Terminate and report results to parent
"""

import asyncio
import logging
import os
import time
from typing import Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorMessage, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools
from lethe.prompts import load_prompt_template

from lethe.paths import workspace_dir as _workspace_dir
WORKSPACE_DIR = str(_workspace_dir())

WORKSPACE_CONTEXT = load_prompt_template(
    "actor_workspace_context",
    fallback=(
        "<workspace>\n"
        "Your workspace is at: {workspace}\n"
        "Home directory: {home}\n"
        "Use absolute paths.\n"
        "</workspace>"
    ),
)

ACTOR_INITIAL_MESSAGE_TEMPLATE = load_prompt_template(
    "actor_initial_message",
    fallback=(
        "You are actor '{actor_name}'. Your goals:\n\n{goals}\n\n{workspace_ctx}\n\n"
        "Begin working. Use your tools to accomplish the task. "
        "When done, call terminate(result) with a detailed summary.\n"
        "If something goes wrong, notify your parent with send_message().\n"
        "If your goals are unclear, use restart_self(new_goals) with a better prompt."
    ),
)

logger = logging.getLogger(__name__)

# Notify parent if task takes longer than this (seconds)
PROGRESS_NOTIFY_INTERVAL = 120


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

    async def _notify_parent(self, message: str, metadata: Optional[dict] = None):
        """Send a status notification to the parent actor."""
        actor = self.actor
        if actor.spawned_by:
            parent = self.registry.get(actor.spawned_by)
            if parent and parent.state != ActorState.TERMINATED:
                msg = ActorMessage(
                    sender=actor.id,
                    recipient=actor.spawned_by,
                    content=message,
                    metadata=dict(metadata or {}),
                )
                try:
                    await parent.send(msg)
                except Exception as e:
                    logger.warning(f"Failed to notify parent: {e}")

    async def _progress_timer(self, start_time: float):
        """Background timer that sends progress updates to parent every 2 minutes."""
        actor = self.actor
        await asyncio.sleep(PROGRESS_NOTIFY_INTERVAL)  # First update after 2 min
        while actor.state != ActorState.TERMINATED:
            elapsed = int(time.monotonic() - start_time)
            turn = actor._turns
            last_response = getattr(actor, '_last_response', '') or ''
            await self._notify_parent(
                (
                    f"{actor.config.name} still working (turn {turn}/{actor.config.max_turns}, "
                    f"{elapsed}s elapsed). Last: {last_response[:100] if last_response else 'working...'}"
                ),
                metadata={"channel": "task_update", "kind": "progress"},
            )
            actor.registry.emit_event(
                "actor_progress",
                actor,
                {"turn": turn, "max_turns": actor.config.max_turns, "elapsed_seconds": elapsed},
            )
            logger.info(f"Actor {actor.id} progress: turn {turn}, {elapsed}s elapsed")
            await asyncio.sleep(PROGRESS_NOTIFY_INTERVAL)

    async def run(self) -> str:
        """Run the actor's LLM loop until completion or max turns."""
        actor = self.actor
        start_time = time.monotonic()
        progress_task: Optional[asyncio.Task] = None
        
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
            if actor._last_prompt_stats:
                logger.info(
                    f"Actor {actor.id} prompt budget: "
                    f"visible {actor._last_prompt_stats['visible_included']}/{actor._last_prompt_stats['visible_total']}, "
                    f"inbox {actor._last_prompt_stats['inbox_included']}/{actor._last_prompt_stats['inbox_total']}"
                )
            
            workspace_ctx = WORKSPACE_CONTEXT.format(
                workspace=WORKSPACE_DIR,
                home=os.path.expanduser("~"),
            )
            
            initial_message = ACTOR_INITIAL_MESSAGE_TEMPLATE.format(
                actor_name=actor.config.name,
                goals=actor.config.goals,
                workspace_ctx=workspace_ctx,
            )
            
            logger.info(f"Actor {actor.id} ({actor.config.name}) starting, tools: {len(llm._tools)}")
            
            # Start background progress timer
            progress_task = asyncio.create_task(
                self._progress_timer(start_time),
                name=f"progress-{actor.id}",
            )
            
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
                    if incoming:
                        parts = []
                        for msg in incoming:
                            sender = self.registry.get(msg.sender)
                            sender_name = sender.config.name if sender else msg.sender
                            parts.append(f"[Message from {sender_name}]: {msg.content}")
                        incoming_text = "\n".join(parts)
                        message = f"{initial_message}\n\nYou have new messages:\n{incoming_text}"
                    else:
                        message = initial_message
                elif incoming:
                    parts = []
                    for msg in incoming:
                        sender = self.registry.get(msg.sender)
                        sender_name = sender.config.name if sender else msg.sender
                        parts.append(f"[Message from {sender_name}]: {msg.content}")
                    message = "\n".join(parts)
                else:
                    # No incoming messages — check if subagent should wrap up
                    if turn >= actor.config.max_turns * 0.7:
                        message = (
                            f"[Turn {turn + 1}/{actor.config.max_turns} — you're running low on turns. "
                            f"Call terminate(result) with your findings NOW.]"
                        )
                    elif turn > 0 and turn % 5 == 0:
                        message = (
                            f"[Turn {turn + 1}/{actor.config.max_turns}. "
                            f"If you have results, call terminate(result). Otherwise continue.]"
                        )
                    else:
                        message = "[Continue working on your goals. Call terminate(result) when done.]"
                
                # Call LLM
                try:
                    response = await llm.chat(message)
                    actor._last_response = response  # For progress timer
                except Exception as e:
                    logger.error(f"Actor {actor.id} LLM error: {e}")
                    actor.terminate(f"Error: {e}")
                    break
                
                if actor.state == ActorState.TERMINATED:
                    break
                
                # Brief pause to allow inbox to accumulate without consuming messages.
                # Messages are drained at the top of each turn.
                await asyncio.sleep(1.0)
            
            # Force terminate if max turns reached
            if actor.state != ActorState.TERMINATED:
                elapsed = time.monotonic() - start_time
                result = f"Max turns reached ({actor.config.max_turns} turns, {int(elapsed)}s). Last: {response[:200] if response else 'none'}"
                logger.warning(f"Actor {actor.id} hit max turns")
                actor.terminate(result)
            
        except Exception as e:
            logger.error(f"Actor {actor.id} runner error: {e}", exc_info=True)
            actor.terminate(f"Runner error: {e}")
        finally:
            # Cancel progress timer
            if progress_task and not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
        
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
