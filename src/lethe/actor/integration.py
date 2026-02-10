"""Integration layer — connects actors to the existing Agent/LLM system.

The cortex (principal) is a pure coordinator — it NEVER calls tools itself.
All work is delegated to subagents who have the actual tools.
Cortex only has actor tools + memory tools + telegram tools.

The DMN (Default Mode Network) is a persistent background subagent that
replaces heartbeats. It scans goals, reorganizes memory, self-improves,
and notifies the cortex when something needs user attention.
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools
from lethe.actor.runner import ActorRunner
from lethe.actor.dmn import DefaultModeNetwork
from lethe.memory.llm import AsyncLLMClient, LLMConfig

logger = logging.getLogger(__name__)

# Tools the cortex keeps (actor + memory + telegram)
# Everything else (file, CLI, web, browser) goes to subagents
CORTEX_TOOL_NAMES = {
    # Actor tools (added by actor system)
    'send_message', 'wait_for_response', 'discover_actors',
    'terminate', 'spawn_actor', 'kill_actor', 'ping_actor',
    # CLI + file tools (cortex handles quick tasks directly)
    'bash', 'read_file', 'write_file', 'edit_file',
    'list_directory', 'grep_search',
    # Memory tools (cortex manages its own memory)
    'memory_read', 'memory_update', 'memory_append',
    'archival_search', 'archival_insert', 'conversation_search',
    # Telegram tools (cortex talks to user)
    'telegram_send_message', 'telegram_send_file',
}

# Tools that ALL subagents always get (CLI + file are fundamental)
SUBAGENT_DEFAULT_TOOLS = {
    'bash', 'read_file', 'write_file', 'edit_file',
    'list_directory', 'grep_search',
}


class ActorSystem:
    """Manages the actor system, wiring it into the existing Agent.
    
    The cortex (principal) only gets actor tools — no file, web, or CLI tools.
    Those are collected and passed to subagents when they're spawned.
    """

    def __init__(self, agent):
        self.agent = agent
        self.registry = ActorRegistry()
        self.principal: Optional[Actor] = None
        self.dmn: Optional[DefaultModeNetwork] = None
        self._background_tasks: Dict[str, asyncio.Task] = {}
        
        # Tools from the agent that subagents can use (not the cortex)
        self._available_tools: Dict[str, tuple] = {}
        
        # Callbacks set by main.py
        self._send_to_user: Optional[Callable] = None
        self._get_reminders: Optional[Callable] = None

    async def setup(self):
        """Set up the actor system.
        
        1. Collect agent's tools for subagent use
        2. Strip non-actor tools from the agent's LLM (cortex doesn't use them)
        3. Create principal actor
        4. Register actor tools with the agent
        """
        # Collect all agent tools BEFORE stripping them
        self._collect_available_tools()
        
        # Strip tools cortex doesn't need — keeps memory + telegram
        if hasattr(self.agent, 'llm') and hasattr(self.agent.llm, '_tools'):
            to_strip = [name for name in self.agent.llm._tools if name not in CORTEX_TOOL_NAMES]
            for name in to_strip:
                del self.agent.llm._tools[name]
            if to_strip:
                logger.info(f"Stripped {len(to_strip)} tools from cortex: {to_strip}")
        
        # Create principal actor
        self.principal = self.registry.spawn(
            ActorConfig(
                name="cortex",
                group="main",
                goals="Serve the user. Handle quick tasks directly. Delegate long or complex tasks to subagents.",
            ),
            is_principal=True,
        )
        
        # Set up LLM factory
        self.registry.set_llm_factory(self._create_llm_for_actor)
        
        # Register actor tools with the cortex's LLM
        actor_tools = create_actor_tools(self.principal, self.registry)
        for func, _ in actor_tools:
            self.agent.add_tool(func)
        
        # Hook spawn to auto-start actors in background
        original_spawn = self.registry.spawn
        def spawn_and_start(*args, **kwargs):
            actor = original_spawn(*args, **kwargs)
            if not actor.is_principal:
                self._start_actor(actor)
            return actor
        self.registry.spawn = spawn_and_start
        
        # Rebuild tool reference in system prompt (was built before stripping)
        if hasattr(self.agent, 'llm'):
            self.agent.llm.context._tool_reference = self.agent.llm.context._build_tool_reference(self.agent.llm.tools)
            self.agent.llm._update_tool_budget()
            logger.info(f"Rebuilt tool reference ({len(self.agent.llm.context._tool_reference)} chars)")
        
        # Initialize DMN (Default Mode Network) — persistent background thinker
        self.dmn = DefaultModeNetwork(
            registry=self.registry,
            llm_factory=self._create_llm_for_actor,
            available_tools=self._available_tools,
            cortex_id=self.principal.id,
            send_to_user=self._send_to_user or (lambda msg: asyncio.sleep(0)),
            get_reminders=self._get_reminders,
        )
        
        tool_count = len(self.agent.llm._tools)
        available_count = len(self._available_tools)
        logger.info(
            f"Actor system initialized. Principal: {self.principal.id}, "
            f"cortex tools: {tool_count}, subagent tools available: {available_count}, DMN ready"
        )

    # Tools subagents must NOT have — they communicate via actors only
    SUBAGENT_EXCLUDED_TOOLS = {
        'telegram_send_message', 'telegram_send_file', 'telegram_react',
    }

    def _collect_available_tools(self):
        """Collect tools from the agent for subagent use.
        
        This runs BEFORE stripping cortex tools, so it captures everything.
        Subagents can request any tool EXCEPT telegram (they message actors, not users).
        """
        if hasattr(self.agent, 'llm') and hasattr(self.agent.llm, '_tools'):
            for name, (func, schema) in self.agent.llm._tools.items():
                if name not in self.SUBAGENT_EXCLUDED_TOOLS:
                    self._available_tools[name] = (func, schema)

    def get_available_tool_names(self) -> List[str]:
        """List tool names available for subagents."""
        return sorted(self._available_tools.keys())

    async def _create_llm_for_actor(self, actor: Actor) -> AsyncLLMClient:
        """Create an LLM client for a subagent actor."""
        config = LLMConfig()
        if actor.config.model:
            config.model = actor.config.model
        else:
            config.model = config.model_aux
        
        config.context_limit = min(config.context_limit, 64000)
        config.max_output_tokens = min(config.max_output_tokens, 4096)
        
        client = AsyncLLMClient(
            config=config,
            system_prompt=actor.build_system_prompt(),
        )
        
        return client

    def _start_actor(self, actor: Actor):
        """Start a non-principal actor running in the background."""
        async def _run():
            try:
                runner = ActorRunner(
                    actor=actor,
                    registry=self.registry,
                    llm_factory=self._create_llm_for_actor,
                    available_tools=self._available_tools,
                )
                result = await runner.run()
                logger.info(f"Actor {actor.config.name} (id={actor.id}) finished: {result[:80]}...")
            except Exception as e:
                logger.error(f"Actor {actor.config.name} (id={actor.id}) error: {e}", exc_info=True)
                if actor.state != ActorState.TERMINATED:
                    actor.terminate(f"Error: {e}")
            finally:
                self._background_tasks.pop(actor.id, None)
        
        task = asyncio.create_task(_run(), name=f"actor-{actor.id}-{actor.config.name}")
        self._background_tasks[actor.id] = task
        actor._task = task
        logger.info(f"Started background actor: {actor.config.name} (id={actor.id})")

    def set_callbacks(
        self,
        send_to_user: Callable,
        get_reminders: Optional[Callable] = None,
    ):
        """Set callbacks for DMN and actor system.
        
        Args:
            send_to_user: async Callable(message: str) -> None
            get_reminders: async Callable() -> str
        """
        self._send_to_user = send_to_user
        self._get_reminders = get_reminders
        if self.dmn:
            self.dmn.send_to_user = send_to_user
            self.dmn.get_reminders = get_reminders

    async def dmn_round(self) -> Optional[str]:
        """Run a DMN round. Called by heartbeat timer.
        
        Returns:
            Message to send to user, or None
        """
        if self.dmn is None:
            return None
        return await self.dmn.run_round()

    async def shutdown(self):
        """Shut down all actors gracefully."""
        logger.info(f"Shutting down actor system ({self.registry.active_count} active actors)")
        
        for actor in list(self.registry._actors.values()):
            if not actor.is_principal and actor.state != ActorState.TERMINATED:
                actor.terminate("System shutdown")
        
        tasks = list(self._background_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        if self.principal and self.principal.state != ActorState.TERMINATED:
            self.principal.terminate("System shutdown")
        
        self.registry.cleanup_terminated(force=True)
        logger.info("Actor system shut down")

    @property
    def status(self) -> dict:
        return {
            "active_actors": self.registry.active_count,
            "background_tasks": len(self._background_tasks),
            "actors": [
                {
                    "id": a.id,
                    "name": a.name,
                    "group": a.group,
                    "state": a.state.value,
                    "goals": a.goals[:80],
                }
                for a in self.registry.all_actors
            ],
        }
