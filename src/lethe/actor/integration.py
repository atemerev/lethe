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
from typing import Awaitable, Callable, Dict, List, Optional

from lethe.actor import Actor, ActorConfig, ActorMessage, ActorRegistry, ActorState
from lethe.actor.tools import create_actor_tools
from lethe.actor.runner import ActorRunner
from lethe.actor.dmn import DefaultModeNetwork
# Amygdala merged into hippocampus — salience tagging now runs per-message
from lethe.actor.brainstem import Brainstem
from lethe.actor.consolidation import MemoryConsolidation
from lethe.config import Settings, get_settings
from lethe.memory.llm import AsyncLLMClient, LLMConfig

logger = logging.getLogger(__name__)

# Tools the cortex keeps (hybrid mode: actor + quick CLI/file + memory + telegram)
CORTEX_TOOL_NAMES = {
    # Core tools (always registered — keep under 15 for Gemma 4)
    'bash', 'read_file', 'write_file', 'edit_file',
    'list_directory', 'grep_search',
    'web_search', 'note_search', 'note_create',
    'telegram_send_message',
    'request_tool',
    # Actor essentials
    'spawn_actor', 'send_message', 'discover_actors',
    # Memory essentials
    'memory_read',
}

# Tools that request_tool() can activate for cortex (kept in CORTEX_TOOL_NAMES
# so they survive the stripping phase, but only registered on demand)
CORTEX_EXTENDED_TOOL_NAMES = {
    'fetch_webpage', 'telegram_send_file', 'telegram_react',
    'browser_open', 'browser_snapshot', 'browser_click', 'browser_fill',
    'memory_append', 'archival_search', 'archival_insert', 'conversation_search',
    'kill_actor', 'ping_actor', 'terminate',
    'note_list',
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
        self.amygdala = None  # Deprecated: salience tagging merged into hippocampus
        self.consolidation: Optional[MemoryConsolidation] = None
        self._background_tasks: Dict[str, asyncio.Task] = {}
        self._principal_monitor_task: Optional[asyncio.Task] = None
        self._processed_principal_message_ids: set[str] = set()
        self._last_principal_message_idx = 0
        
        # Tools from the agent that subagents can use (not the cortex)
        self._available_tools: Dict[str, tuple] = {}
        
        # Callbacks set by main.py
        self._send_to_user: Optional[Callable] = None
        self._get_reminders: Optional[Callable] = None
        self._decide_user_notify: Optional[Callable[[str, str, dict], Awaitable[Optional[str]]]] = None
        self._run_cortex_turn: Optional[Callable[[str], Awaitable[None]]] = None

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
        # Collect all agent tools BEFORE stripping them (for subagent use)
        self._collect_available_tools()

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

        # Strip tools down to CORTEX_TOOL_NAMES (keep under 15 for Gemma 4).
        # Stash stripped tools in _EXTENDED_TOOLS so request_tool() can activate them.
        if hasattr(self.agent, 'llm') and hasattr(self.agent.llm, '_tools'):
            from lethe.tools import _EXTENDED_TOOLS
            for name in list(self.agent.llm._tools.keys()):
                if name not in CORTEX_TOOL_NAMES:
                    func, schema = self.agent.llm._tools[name]
                    if name not in _EXTENDED_TOOLS:
                        _EXTENDED_TOOLS[name] = (func, None)
            to_strip = [name for name in self.agent.llm._tools if name not in CORTEX_TOOL_NAMES]
            for name in to_strip:
                del self.agent.llm._tools[name]
            self.agent.llm._update_tool_budget()
            logger.info(f"Cortex tools: {len(self.agent.llm._tools)} (stripped {len(to_strip)}: {sorted(to_strip)})")

        # Wire principal actor into the agent so it can drain inbox and see actor context.
        self.agent._principal_actor = self.principal
        def _principal_actor_context() -> str:
            if self.principal:
                return self.principal.build_system_prompt()
            return ""
        self.agent._actor_context_provider = _principal_actor_context

        # Hook spawn to auto-start actors in background
        original_spawn = self.registry.spawn
        def spawn_and_start(*args, **kwargs):
            actor = original_spawn(*args, **kwargs)
            # Auto-start non-principal actors, but NOT background supervisors.
            if not actor.is_principal and actor.config.name not in {"brainstem", "dmn", "amygdala", "consolidation"}:
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
        # Amygdala removed: salience tagging now runs per-message in hippocampus
        
        # Initialize Memory Consolidation — slow-cadence memory compression
        if getattr(self.settings, "consolidation_enabled", True):
            self.consolidation = MemoryConsolidation(
                registry=self.registry,
                llm_factory=self._create_llm_for_actor,
                available_tools=self._available_tools,
                cortex_id=self.principal.id,
                send_to_user=self._send_to_user or (lambda msg: asyncio.sleep(0)),
            )

        tool_count = len(self.agent.llm._tools)
        available_count = len(self._available_tools)
        amygdala_state = "merged into hippocampus"
        consolidation_state = "enabled" if self.consolidation else "disabled"
        logger.info(
            f"Actor system initialized. Principal: {self.principal.id}, "
            f"cortex tools: {tool_count}, subagent tools available: {available_count}, "
            f"Brainstem online, DMN ready, Amygdala {amygdala_state}, "
            f"Consolidation {consolidation_state}"
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

        # Subagent task updates — trigger a cortex turn so it can decide how to respond.
        _TERMINAL_KINDS = {"done", "failed", "error", "max_turns"}
        _NOTIFY_KINDS = _TERMINAL_KINDS | {"progress"}
        channel = metadata.get("channel", "")
        kind = metadata.get("kind", "")
        logger.info(
            "Principal message from %s: channel=%s kind=%s content=%s",
            sender_name, channel, kind, content[:120],
        )
        if (
            channel == "task_update"
            and kind in _NOTIFY_KINDS
            and sender_name not in {"brainstem", "dmn", "amygdala"}
        ):
            if kind in _TERMINAL_KINDS:
                synthetic = (
                    f"[System: subagent '{sender_name}' finished ({kind}). "
                    f"Its result is in your inbox. Review it and respond to the user.]"
                )
            else:
                synthetic = (
                    f"[System: subagent '{sender_name}' progress update: {content[:200]}. "
                    f"Let the user know if appropriate.]"
                )
            if self._run_cortex_turn:
                logger.info("Triggering cortex turn for subagent '%s' %s", sender_name, kind)
                try:
                    await self._run_cortex_turn(synthetic)
                except Exception as e:
                    logger.warning("Cortex turn for subagent %s failed: %s", kind, e)
            else:
                logger.warning("No run_cortex_turn callback; subagent '%s' %s stuck in inbox", sender_name, kind)
            return

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
        if not self._decide_user_notify or not self._send_to_user:
            return
        try:
            relay_message = await self._decide_user_notify(sender_name, content, metadata)
            relay_text = (relay_message or "").strip()
            if not relay_text:
                if self.principal:
                    self.registry.emit_event(
                        "background_notify_dropped_by_cortex",
                        self.principal,
                        {
                            "from_actor_id": message.sender,
                            "from_actor_name": sender_name,
                            "message_preview": content[:240],
                        },
                    )
                return
            await self._send_to_user(relay_text)
            if self.principal:
                self.registry.emit_event(
                    "background_notify_relayed_to_user",
                    self.principal,
                    {
                        "from_actor_id": message.sender,
                        "from_actor_name": sender_name,
                        "message_preview": relay_text[:240],
                    },
                )
        except Exception as e:
            logger.warning("Cortex notify decision failed: %s", e)
            if self.principal:
                self.registry.emit_event(
                    "background_notify_decision_error",
                    self.principal,
                    {
                        "from_actor_id": message.sender,
                        "from_actor_name": sender_name,
                        "error": str(e),
                    },
                )
        return

    def set_callbacks(
        self,
        send_to_user: Callable,
        get_reminders: Optional[Callable] = None,
        decide_user_notify: Optional[Callable[[str, str, dict], Awaitable[Optional[str]]]] = None,
        run_cortex_turn: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """Set callbacks for DMN and actor system.

        Args:
            send_to_user: async Callable(message: str) -> None
            get_reminders: async Callable() -> str
            decide_user_notify: async Callable(from_actor, message, metadata) -> relay message or None
            run_cortex_turn: async Callable(synthetic_message: str) -> None — triggers a full cortex LLM turn
        """
        self._send_to_user = send_to_user
        self._get_reminders = get_reminders
        self._decide_user_notify = decide_user_notify
        self._run_cortex_turn = run_cortex_turn
        if self.dmn:
            self.dmn.send_to_user = send_to_user
            self.dmn.get_reminders = get_reminders
        # Amygdala removed — salience tagging in hippocampus doesn't need send_to_user
        if self.consolidation:
            self.consolidation.send_to_user = send_to_user

    async def dmn_round(self) -> Optional[str]:
        """Run a DMN round. Called by heartbeat timer.
        
        Returns:
            Message to send to user, or None
        """
        if self.dmn is None:
            return None
        return await self.dmn.run_round()

    async def consolidation_round(self) -> Optional[str]:
        """Run a consolidation round. Called by heartbeat timer.

        Self-gating: the module internally checks cadence/capacity triggers.
        """
        if self.consolidation is None:
            return None
        return await self.consolidation.run_round()

    async def brainstem_heartbeat(self, heartbeat_message: str = "") -> Optional[str]:
        """Run Brainstem supervisory checks (main heartbeat cadence)."""
        if self.brainstem is None:
            return None
        await self.brainstem.heartbeat(heartbeat_message=heartbeat_message)
        return None

    async def background_round(self) -> Optional[str]:
        """Run background cognition rounds (DMN + Consolidation).

        Amygdala salience tagging now runs per-message in hippocampus.
        """
        dmn_result = await self.dmn_round()
        await self.consolidation_round()  # self-gating, no user-facing output
        return dmn_result

    async def shutdown(self):
        """Shut down all actors gracefully."""
        logger.info(f"Shutting down actor system ({self.registry.active_count} active actors)")
        if self.brainstem:
            try:
                self.brainstem.record_shutdown()
            except Exception as e:
                logger.debug("Brainstem shutdown marker failed: %s", e)
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
        amygdala_status = {}  # Deprecated: merged into hippocampus
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
                return text[:500]

            lines = []
            for item in user_messages[-8:]:
                created = (item.get("created_at") or "")[:19]
                lines.append(f"- [{created}] {_clean(item.get('content', ''))}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Failed to build recent user signals: {e}")
            return f"(failed to build user signals: {e})"
