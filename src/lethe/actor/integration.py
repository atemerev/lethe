"""Integration layer — connects actors to the existing Agent/LLM system.

The cortex (principal) runs in intentional hybrid mode:
- Handle quick local tasks directly (CLI/file/memory/telegram)
- Delegate long or parallel work to subagents

The DMN (Default Mode Network) is a persistent background subagent that
replaces heartbeats. It scans goals, reorganizes memory, self-improves,
and notifies the cortex when something needs user attention.
"""

import asyncio
import json
import logging
import os
from typing import Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorMessage, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools
from lethe.actor.runner import ActorRunner
from lethe.actor.dmn import DefaultModeNetwork
from lethe.actor.amygdala import Amygdala
from lethe.actor.brainstem import Brainstem
from lethe.config import Settings, get_settings
from lethe.memory.llm import AsyncLLMClient, LLMConfig

logger = logging.getLogger(__name__)

# Tools the cortex keeps (hybrid mode: actor + quick CLI/file + memory + telegram)
CORTEX_TOOL_NAMES = {
    # Actor tools (added by actor system)
    'send_message', 'wait_for_response', 'discover_actors',
    'terminate', 'spawn_actor', 'kill_actor', 'ping_actor',
    # CLI + file tools (cortex handles quick tasks directly)
    'bash', 'read_file', 'write_file', 'edit_file',
    'list_directory', 'grep_search',
    # Web + browser tools (cortex can handle quick online tasks directly)
    'web_search', 'fetch_webpage',
    'browser_open', 'browser_snapshot', 'browser_click', 'browser_fill',
    # Memory tools (cortex manages its own memory)
    'memory_read', 'memory_update', 'memory_append',
    'archival_search', 'archival_insert', 'conversation_search',
    # Telegram tools (cortex talks to user)
    'telegram_send_message', 'telegram_send_file', 'telegram_react',
    # Local image inspection/sending
    'view_image', 'send_image',
}

# Tools that ALL subagents always get (CLI + file are fundamental)
SUBAGENT_DEFAULT_TOOLS = {
    'bash', 'read_file', 'write_file', 'edit_file',
    'list_directory', 'grep_search', 'view_image',
}


class ActorSystem:
    """Manages the actor system, wiring it into the existing Agent.
    
    The cortex (principal) is hybrid: quick local tasks directly, long work delegated.
    Subagents still get the broad tool surface for deeper/parallel tasks.
    """

    def __init__(self, agent, settings: Optional[Settings] = None):
        self.agent = agent
        self.settings = settings or get_settings()
        self.registry = ActorRegistry()
        self.principal: Optional[Actor] = None
        self.brainstem: Optional[Brainstem] = None
        self.dmn: Optional[DefaultModeNetwork] = None
        self.amygdala: Optional[Amygdala] = None
        self._background_tasks: Dict[str, asyncio.Task] = {}
        self._principal_monitor_task: Optional[asyncio.Task] = None
        self._processed_principal_message_ids: set[str] = set()
        self._last_principal_message_idx = 0
        
        # Tools from the agent that subagents can use (not the cortex)
        self._available_tools: Dict[str, tuple] = {}
        
        # Callbacks set by main.py
        self._send_to_user: Optional[Callable] = None
        self._get_reminders: Optional[Callable] = None

    def _get_principal_context(self) -> str:
        """Build principal context for DMN from live memory blocks."""
        try:
            blocks = getattr(self.agent, "memory", None).blocks
            identity = blocks.get("identity") or {}
            human = blocks.get("human") or {}
            project = blocks.get("project") or {}

            def _extract(value: str, max_lines: int = 40) -> str:
                text = (value or "").strip()
                if not text:
                    return ""
                lines = text.splitlines()
                if len(lines) <= max_lines:
                    return text
                return "\n".join(lines[:max_lines]) + "\n...[truncated by lines]"

            parts = []
            if identity.get("value"):
                parts.append(f"Identity snapshot:\n{_extract(identity.get('value', ''))}")
            if human.get("value"):
                parts.append(f"Human context:\n{_extract(human.get('value', ''))}")
            if project.get("value"):
                parts.append(f"Project context:\n{_extract(project.get('value', ''))}")
            if not parts:
                return (
                    "Advance your principal's goals with current memory context. "
                    "If context is missing, prioritize building fresh actionable context."
                )
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning(f"Failed to build principal context for DMN: {e}")
            return "Advance your principal's goals based on current memory and recent activity."

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
            # Auto-start non-principal actors, but NOT background supervisors.
            if not actor.is_principal and actor.config.name not in {"brainstem", "dmn", "amygdala"}:
                self._start_actor(actor)
            return actor
        self.registry.spawn = spawn_and_start
        
        # Rebuild tool reference in system prompt (was built before stripping)
        if hasattr(self.agent, 'llm'):
            self.agent.llm.context._tool_reference = self.agent.llm.context._build_tool_reference(self.agent.llm.tools)
            self.agent.llm._update_tool_budget()
            logger.info(f"Rebuilt tool reference ({len(self.agent.llm.context._tool_reference)} chars)")
        
        # Initialize Brainstem FIRST. It supervises boot and runtime health.
        self.brainstem = Brainstem(
            registry=self.registry,
            settings=self.settings,
            cortex_id=self.principal.id,
        )
        await self.brainstem.startup()

        # Initialize DMN (Default Mode Network) — persistent background thinker
        self.dmn = DefaultModeNetwork(
            registry=self.registry,
            llm_factory=self._create_llm_for_actor,
            available_tools=self._available_tools,
            cortex_id=self.principal.id,
            send_to_user=self._send_to_user or (lambda msg: asyncio.sleep(0)),
            get_reminders=self._get_reminders,
            principal_context_provider=self._get_principal_context,
            model_override=self.settings.llm_model_dmn,
        )
        if self.settings.amygdala_enabled:
            self.amygdala = Amygdala(
                registry=self.registry,
                available_tools=self._available_tools,
                cortex_id=self.principal.id,
                send_to_user=self._send_to_user or (lambda msg: asyncio.sleep(0)),
                recent_signals_provider=self._get_recent_user_signals,
                principal_context_provider=self._get_principal_context,
            )
        
        tool_count = len(self.agent.llm._tools)
        available_count = len(self._available_tools)
        amygdala_state = "enabled" if self.amygdala else "disabled"
        logger.info(
            f"Actor system initialized. Principal: {self.principal.id}, "
            f"cortex tools: {tool_count}, subagent tools available: {available_count}, "
            f"Brainstem online, DMN ready, Amygdala {amygdala_state}"
        )
        self._start_principal_monitor()

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
            usage_scope=f"actor:{actor.config.name}",
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

    def _start_principal_monitor(self):
        """Monitor principal inbox/messages even when cortex is not in an active LLM loop."""
        if self._principal_monitor_task and not self._principal_monitor_task.done():
            return

        async def _monitor():
            while True:
                try:
                    await asyncio.sleep(1.0)
                    if not self.principal or self.principal.state == ActorState.TERMINATED:
                        continue
                    all_messages = self.principal._messages
                    if self._last_principal_message_idx >= len(all_messages):
                        continue
                    new_messages = all_messages[self._last_principal_message_idx:]
                    self._last_principal_message_idx = len(all_messages)
                    for msg in new_messages:
                        if msg.id in self._processed_principal_message_ids:
                            continue
                        if msg.recipient != self.principal.id:
                            continue
                        if msg.sender == self.principal.id:
                            continue
                        self._processed_principal_message_ids.add(msg.id)
                        await self._handle_principal_message(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Principal monitor error: {e}")

        self._principal_monitor_task = asyncio.create_task(_monitor(), name="actor-principal-monitor")

    async def _handle_principal_message(self, message: ActorMessage):
        """Record child->principal updates without bypassing cortex."""
        content = (message.content or "").strip()
        metadata = message.metadata or {}
        sender = self.registry.get(message.sender)
        sender_name = sender.config.name if sender else message.sender
        # Route stays actor->cortex; cortex decides whether/how to message the user.
        if self.principal:
            self.registry.emit_event(
                "principal_update_received",
                self.principal,
                {
                    "from_actor_id": message.sender,
                    "from_actor_name": sender_name,
                    "message_id": message.id,
                    "content_preview": content[:240],
                    "channel": metadata.get("channel", ""),
                    "kind": metadata.get("kind", ""),
                },
            )
        logger.debug(
            "Principal update received from %s (%s): %s",
            sender_name,
            message.sender,
            content[:180],
        )

        # Background processes can ask cortex to notify the user.
        # System actors never bypass cortex: relay decision is always cortex-owned.
        if metadata.get("channel") != "user_notify":
            return
        if sender_name not in {"brainstem", "dmn", "amygdala"}:
            return
        if self.principal:
            self.registry.emit_event(
                "background_notify_deferred_to_cortex",
                self.principal,
                {
                    "from_actor_id": message.sender,
                    "from_actor_name": sender_name,
                    "message_preview": content[:240],
                },
            )
        return

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
        if self.amygdala:
            self.amygdala.send_to_user = send_to_user

    async def dmn_round(self) -> Optional[str]:
        """Run a DMN round. Called by heartbeat timer.
        
        Returns:
            Message to send to user, or None
        """
        if self.dmn is None:
            return None
        return await self.dmn.run_round()

    async def amygdala_round(self) -> Optional[str]:
        """Run an Amygdala round. Called by heartbeat timer."""
        if self.amygdala is None:
            return None
        return await self.amygdala.run_round()

    async def brainstem_heartbeat(self, heartbeat_message: str = "") -> Optional[str]:
        """Run Brainstem supervisory checks (main heartbeat cadence)."""
        if self.brainstem is None:
            return None
        await self.brainstem.heartbeat(heartbeat_message=heartbeat_message)
        return None

    async def background_round(self) -> Optional[str]:
        """Run background cognition rounds (DMN + Amygdala)."""
        dmn_result = await self.dmn_round()
        amygdala_result = await self.amygdala_round()
        return dmn_result or amygdala_result

    async def shutdown(self):
        """Shut down all actors gracefully."""
        logger.info(f"Shutting down actor system ({self.registry.active_count} active actors)")
        if self._principal_monitor_task and not self._principal_monitor_task.done():
            self._principal_monitor_task.cancel()
            try:
                await self._principal_monitor_task
            except asyncio.CancelledError:
                pass
        
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
        all_events = self.registry.events.query(limit=500)
        recent_events = all_events[-10:]
        lifecycle_events = [
            e for e in all_events
            if e.event_type in {"actor_spawned", "actor_terminated"}
        ][-30:]
        dmn_status = self.dmn.status if self.dmn else {}
        amygdala_status = self.amygdala.status if self.amygdala else {}
        brainstem_status = self.brainstem.status if self.brainstem else {}
        actor_last_event_at: dict[str, str] = {}
        for e in all_events:
            actor_last_event_at[e.actor_id] = e.created_at.isoformat()
        return {
            "active_actors": self.registry.active_count,
            "background_tasks": len(self._background_tasks),
            "principal_monitor_running": bool(
                self._principal_monitor_task and not self._principal_monitor_task.done()
            ),
            "actors": [
                {
                    "id": a.id,
                    "name": a.name,
                    "group": a.group,
                    "state": a.state.value,
                    "task_state": a.task_state.value,
                    "goals": a.goals[:80],
                }
                for a in self.registry.all_actors
            ],
            "actor_last_event_at": actor_last_event_at,
            "recent_events": [
                {
                    "type": e.event_type,
                    "actor_id": e.actor_id,
                    "actor_name": (self.registry.get(e.actor_id).config.name if self.registry.get(e.actor_id) else ""),
                    "group": e.group,
                    "payload": e.payload,
                    "created_at": e.created_at.isoformat(),
                }
                for e in recent_events
            ],
            "lifecycle_events": [
                {
                    "type": e.event_type,
                    "actor_id": e.actor_id,
                    "actor_name": (
                        (e.payload.get("name") if isinstance(e.payload, dict) else "")
                        or (self.registry.get(e.actor_id).config.name if self.registry.get(e.actor_id) else "")
                    ),
                    "created_at": e.created_at.isoformat(),
                }
                for e in lifecycle_events
            ],
            "brainstem": brainstem_status,
            "dmn": dmn_status,
            "amygdala": amygdala_status,
        }

    def _get_recent_user_signals(self) -> str:
        """Build compact user signal context for Amygdala rounds."""
        try:
            recent = self.agent.memory.messages.get_recent(limit=14) or []
            user_messages = [m for m in recent if m.get("role") == "user"]
            if not user_messages:
                return "(no recent user messages)"

            def _clean(content: str) -> str:
                text = content or ""
                if text.startswith("[") and text.endswith("]"):
                    # Stored multimodal payload as JSON list.
                    try:
                        data = json.loads(text)
                        if isinstance(data, list):
                            parts = [p.get("text", "") for p in data if isinstance(p, dict) and p.get("type") == "text"]
                            text = " ".join(parts) or text
                    except Exception:
                        pass
                text = " ".join(text.split())
                return text[:220]

            lines = []
            for item in user_messages[-8:]:
                created = (item.get("created_at") or "")[:19]
                lines.append(f"- [{created}] {_clean(item.get('content', ''))}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Failed to build recent user signals: {e}")
            return f"(failed to build user signals: {e})"
