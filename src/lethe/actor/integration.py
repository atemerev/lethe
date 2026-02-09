"""Integration layer — connects actors to the existing Agent/LLM system.

Provides:
- LLM factory for creating lightweight LLM clients for subagent actors
- Principal actor setup (wraps existing Agent as the butler)
- Background actor execution management
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools
from lethe.actor.runner import ActorRunner, run_actor_in_background
from lethe.memory.llm import AsyncLLMClient, LLMConfig

logger = logging.getLogger(__name__)


class ActorSystem:
    """Manages the actor system, wiring it into the existing Agent.
    
    Usage:
        system = ActorSystem(agent)
        await system.setup()
        # Agent now has actor tools (spawn_subagent, etc.)
        # Subagents run in background with their own LLM clients
    """

    def __init__(self, agent):
        """
        Args:
            agent: The existing Lethe Agent instance (becomes the principal)
        """
        self.agent = agent
        self.registry = ActorRegistry()
        self.principal: Optional[Actor] = None
        self._background_tasks: Dict[str, asyncio.Task] = {}
        
        # Collect available tools from the agent for subagent access
        self._available_tools: Dict[str, tuple] = {}

    async def setup(self):
        """Set up the actor system.
        
        1. Creates principal actor for the existing agent
        2. Registers actor tools with the agent's LLM
        3. Sets up LLM factory for subagents
        """
        # Create principal actor
        self.principal = self.registry.spawn(
            ActorConfig(
                name="butler",
                group="main",
                goals="Serve the user. Delegate complex subtasks to subagents when appropriate.",
            ),
            is_principal=True,
        )
        
        # Set up LLM factory
        self.registry.set_llm_factory(self._create_llm_for_actor)
        
        # Collect tools that subagents can request
        self._collect_available_tools()
        
        # Register actor tools with the principal's (agent's) LLM
        actor_tools = create_actor_tools(self.principal, self.registry)
        for func, _ in actor_tools:
            self.agent.add_tool(func)
        
        # Hook into spawn to auto-start actors
        original_spawn = self.registry.spawn
        def spawn_and_start(*args, **kwargs):
            actor = original_spawn(*args, **kwargs)
            if not actor.is_principal:
                self._start_actor(actor)
            return actor
        self.registry.spawn = spawn_and_start
        
        logger.info(f"Actor system initialized. Principal: {self.principal.id}")

    def _collect_available_tools(self):
        """Collect tools from the agent that subagents can request."""
        if hasattr(self.agent, 'llm') and hasattr(self.agent.llm, '_tools'):
            for name, (func, schema) in self.agent.llm._tools.items():
                # Skip actor-specific tools (they get their own)
                if name in ('send_message', 'wait_for_response', 'discover_actors', 
                           'terminate', 'spawn_subagent', 'kill_actor'):
                    continue
                self._available_tools[name] = (func, schema)

    async def _create_llm_for_actor(self, actor: Actor) -> AsyncLLMClient:
        """Create a lightweight LLM client for a subagent actor."""
        # Use actor's model or fall back to aux model
        config = LLMConfig()
        if actor.config.model:
            config.model = actor.config.model
        else:
            # Use aux model for subagents (cheaper)
            config.model = config.model_aux
        
        # Smaller context for subagents
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
                # Clean up task reference
                self._background_tasks.pop(actor.id, None)
        
        task = asyncio.create_task(_run(), name=f"actor-{actor.id}-{actor.config.name}")
        self._background_tasks[actor.id] = task
        actor._task = task
        logger.info(f"Started background actor: {actor.config.name} (id={actor.id})")

    async def shutdown(self):
        """Shut down all actors gracefully."""
        logger.info(f"Shutting down actor system ({self.registry.active_count} active actors)")
        
        # Terminate all non-principal actors
        for actor in list(self.registry._actors.values()):
            if not actor.is_principal and actor.state != ActorState.TERMINATED:
                actor.terminate("System shutdown")
        
        # Wait for background tasks to finish
        tasks = list(self._background_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        # Terminate principal
        if self.principal and self.principal.state != ActorState.TERMINATED:
            self.principal.terminate("System shutdown")
        
        self.registry.cleanup_terminated()
        logger.info("Actor system shut down")

    @property
    def status(self) -> dict:
        """Get actor system status for monitoring."""
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
