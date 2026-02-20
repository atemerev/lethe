"""Actor model for Lethe — subagents with lifecycles.

Actors are autonomous agents that can:
- Have their own goals, model, and tools
- Discover other actors in their group
- Communicate with parents, siblings, and children
- Spawn child actors for subtasks
- Terminate themselves or their immediate children

The principal actor ("cortex") is the only one that talks to the user.
All other actors communicate through the cortex or with each other.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from lethe.prompts import load_prompt_template

logger = logging.getLogger(__name__)


PRINCIPAL_PROMPT_BLOCK = load_prompt_template(
    "actor_principal_preamble",
    fallback=(
        "You are the cortex - the conscious executive layer, the user's direct interface.\n"
        "You are the ONLY actor that communicates with the user.\n\n"
        "You have CLI and file tools - handle quick tasks DIRECTLY:\n"
        "- Reading files, checking status, running simple commands\n"
        "- Quick edits, searches, directory listings\n"
        "- Anything completable in under a minute\n\n"
        "Spawn a subagent ONLY when:\n"
        "- The task will take more than ~1 minute (multi-step, research, long builds)\n"
        "- It benefits from isolation or parallel execution (long crawling, multi-source research, independent subtasks)\n"
        "- You want parallel execution (multiple independent tasks)\n"
        "Be specific in subagent goals - they only know what you tell them.\n"
        "Monitor subagents with ping_actor(). Kill stuck ones with kill_actor().\n\n"
        "CRITICAL - NEVER spawn duplicates:\n"
        "- ALWAYS call discover_actors() BEFORE spawning to see who's already running\n"
        "- If an actor with similar goals exists, send_message() to it instead\n"
        "- ONE actor per task. Do NOT spawn multiple actors for the same request"
    ),
)

SUBAGENT_PROMPT_BLOCK = load_prompt_template(
    "actor_subagent_preamble",
    fallback=(
        "You are a subagent actor named '{actor_name}'.\n"
        "You were spawned by '{parent_name}' (id={parent_id}) to accomplish a specific task.\n"
        "You CANNOT talk to the user directly. Report your results to the actor that spawned you.\n"
        "If something goes wrong, notify your parent immediately.\n"
        "If your goals are unclear, use restart_self(new_goals) with better goals."
    ),
)

PRINCIPAL_RULES_BLOCK = load_prompt_template(
    "actor_principal_rules",
    fallback=(
        "- Handle quick tasks directly (bash, file ops). Spawn subagents for long/complex work.\n"
        "- Use `spawn_actor(name, goals, tools, ...)` - be DETAILED in goals\n"
        "- Use `ping_actor(actor_id)` to check what a subagent is doing\n"
        "- Use `kill_actor(actor_id)` to terminate a stuck child\n"
        "- Use `send_message(actor_id, content)` to give instructions or ask for status\n"
        "- Use `discover_actors()` to see all active actors\n"
        "- Use `discover_recently_finished()` to inspect recent completed work\n"
        "- Wait for subagent results, then report to the user"
    ),
)

SUBAGENT_RULES_BLOCK = load_prompt_template(
    "actor_subagent_rules",
    fallback=(
        "- Use your tools to accomplish your goals\n"
        "- Use `send_message(actor_id, content)` to message parent, siblings, or children\n"
        "- Use `spawn_actor(...)` if you need to delegate a subtask\n"
        "- Use `update_task_state(state, note)` to checkpoint: planned/running/blocked/done\n"
        "- Use `restart_self(new_goals)` if your goals are unclear or you need a different approach\n"
        "- Report results to your parent '{parent_name}' (id={parent_id}) before terminating\n"
        "- Use `terminate(result)` when done - include a detailed summary\n"
        "- If something goes wrong, notify your parent immediately with send_message()"
    ),
)


class ActorState(str, Enum):
    """Lifecycle states for an actor."""
    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING = "waiting"  # Waiting for response from another actor
    TERMINATED = "terminated"


class TaskState(str, Enum):
    """Task execution states for long-running actors."""
    PLANNED = "planned"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"


@dataclass
class ActorMessage:
    """Message passed between actors."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    sender: str = ""       # Actor ID of sender
    recipient: str = ""    # Actor ID of recipient
    content: str = ""      # Message text
    reply_to: Optional[str] = None  # Message ID this replies to
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def format(self) -> str:
        """Format for inclusion in actor context."""
        dt = self.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.astimezone(timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")
        reply = f" (reply to {self.reply_to})" if self.reply_to else ""
        return f"[{ts}] {self.sender}{reply}: {self.content}"


@dataclass
class ActorEvent:
    """Structured actor runtime event."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_type: str = ""
    actor_id: str = ""
    group: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ActorEventBus:
    """In-memory event bus for actor runtime observability and orchestration."""

    def __init__(self, max_events: int = 1000):
        self.max_events = max_events
        self._events: List[ActorEvent] = []
        self._subscribers: List[Callable[[ActorEvent], Any]] = []

    def subscribe(self, callback: Callable[[ActorEvent], Any]):
        self._subscribers.append(callback)

    def emit(self, event: ActorEvent):
        self._events.append(event)
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events:]
        for callback in self._subscribers:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        pass
            except Exception as e:
                logger.warning(f"Event subscriber failed: {e}")

    def query(
        self,
        event_type: str = "",
        actor_id: str = "",
        group: str = "",
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[ActorEvent]:
        matches = []
        for event in self._events:
            if event_type and event.event_type != event_type:
                continue
            if actor_id and event.actor_id != actor_id:
                continue
            if group and event.group != group:
                continue
            if since and event.created_at < since:
                continue
            matches.append(event)
        return matches[-limit:]


@dataclass
class ActorConfig:
    """Configuration for spawning an actor."""
    name: str                              # Human-readable name (e.g., "researcher")
    group: str = "default"                 # Actor group for discovery
    goals: str = ""                        # What this actor should accomplish
    model: str = ""                        # LLM model override (empty = use aux)
    tools: List[str] = field(default_factory=list)  # Tool names available to this actor
    max_turns: int = 50                    # Max LLM turns before forced termination
    max_messages: int = 50                 # Max inter-actor messages


@dataclass
class ActorInfo:
    """Public information about an actor, visible to other actors in the group."""
    id: str
    name: str
    group: str
    goals: str
    state: ActorState
    task_state: TaskState
    spawned_by: str  # Actor ID that created this one

    def format(self) -> str:
        """Format for inclusion in actor context."""
        return f"- {self.name} (id={self.id}, state={self.state.value}, task={self.task_state.value}): {self.goals}"


class Actor:
    """An autonomous agent with a lifecycle.
    
    Each actor has its own LLM client, tools, goals, and message queue.
    The principal actor is special — it receives user messages and sends
    responses back to the user.
    """

    _TASK_STATE_TRANSITIONS = {
        TaskState.PLANNED: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.DONE},
        TaskState.RUNNING: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.DONE},
        TaskState.BLOCKED: {TaskState.BLOCKED, TaskState.RUNNING, TaskState.DONE},
        TaskState.DONE: {TaskState.DONE},
    }

    _PROMPT_LIMITS = {
        "principal": {"visible_actors": 10, "inbox_messages": 8, "content_chars": 280, "goal_chars": 180},
        "subagent": {"visible_actors": 6, "inbox_messages": 5, "content_chars": 220, "goal_chars": 160},
    }

    def __init__(
        self,
        config: ActorConfig,
        registry: "ActorRegistry",
        spawned_by: Optional[str] = None,
        is_principal: bool = False,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.config = config
        self.registry = registry
        self.spawned_by = spawned_by or ""
        self.is_principal = is_principal
        self.state = ActorState.INITIALIZING
        self.task_state = TaskState.PLANNED
        self.created_at: datetime = datetime.now(timezone.utc)
        self.terminated_at: Optional[datetime] = None
        
        # Message queue (from other actors)
        self._inbox: asyncio.Queue[ActorMessage] = asyncio.Queue()
        # Conversation history (for this actor's LLM context)
        self._messages: List[ActorMessage] = []
        # Result (set when actor terminates)
        self._result: Optional[str] = None
        # Task handle (for async execution)
        self._task: Optional[asyncio.Task] = None
        # LLM client (set by runner or agent integration)
        self._llm = None
        # Turn counter
        self._turns = 0
        self._last_prompt_stats: Dict[str, int] = {}
        
        self.created_at = datetime.now(timezone.utc)
        
        logger.info(f"Actor created: {self.config.name} (id={self.id}, group={self.config.group})")

    @property
    def info(self) -> ActorInfo:
        """Public info visible to other actors."""
        return ActorInfo(
            id=self.id,
            name=self.config.name,
            group=self.config.group,
            goals=self.config.goals,
            state=self.state,
            task_state=self.task_state,
            spawned_by=self.spawned_by,
        )

    def set_task_state(self, state: str, note: str = "") -> tuple[bool, str]:
        """Update task execution state with transition validation."""
        try:
            new_state = TaskState(state)
        except ValueError:
            valid = ", ".join(s.value for s in TaskState)
            return False, f"Invalid task state '{state}'. Valid: {valid}"

        allowed = self._TASK_STATE_TRANSITIONS[self.task_state]
        if new_state not in allowed:
            return False, f"Invalid transition: {self.task_state.value} -> {new_state.value}"

        previous = self.task_state
        self.task_state = new_state
        self.registry.emit_event(
            "task_state_changed",
            self,
            {"from": previous.value, "to": new_state.value, "note": note},
        )
        return True, f"Task state updated: {previous.value} -> {new_state.value}"

    def can_message(self, target_id: str) -> bool:
        """Check if this actor can message another.
        
        Actors can message their:
        - Parent (spawned_by)
        - Siblings (same spawned_by)
        - Children (spawned by self)
        - Group members (same group)
        """
        target = self.registry.get(target_id)
        if target is None:
            return False
        # Parent
        if target_id == self.spawned_by:
            return True
        # Child
        if target.spawned_by == self.id:
            return True
        # Sibling (same parent)
        if self.spawned_by and target.spawned_by == self.spawned_by:
            return True
        # Same group
        if target.config.group == self.config.group:
            return True
        # Principal can message anyone
        if self.is_principal:
            return True
        return False

    async def send(self, message: ActorMessage):
        """Receive a message from another actor."""
        self._messages.append(message)
        await self._inbox.put(message)
        logger.debug(f"Actor {self.id} received message from {message.sender}: {message.content[:50]}...")

    async def send_to(
        self,
        recipient_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ActorMessage:
        """Send a message to another actor."""
        recipient = self.registry.get(recipient_id)
        if recipient is None:
            raise ValueError(f"Actor {recipient_id} not found")
        if not self.can_message(recipient_id):
            raise PermissionError(f"Actor {self.id} cannot message actor {recipient_id} (not related)")
        msg = ActorMessage(
            sender=self.id,
            recipient=recipient_id,
            content=content,
            reply_to=reply_to,
            metadata=dict(metadata or {}),
        )
        await recipient.send(msg)
        self._messages.append(msg)
        preview = content[:200]
        self.registry.emit_event(
            "actor_message",
            self,
            {
                "recipient": recipient_id,
                "message_id": msg.id,
                "content_preview": preview,
                "channel": msg.metadata.get("channel", ""),
                "kind": msg.metadata.get("kind", ""),
            },
        )
        channel = str(msg.metadata.get("channel", "")).strip()
        if channel == "user_notify":
            notify = content.strip()
            self.registry.emit_event(
                "user_notify",
                self,
                {"recipient": recipient_id, "message_id": msg.id, "message": notify},
            )
        return msg

    async def wait_for_reply(self, timeout: float = 120.0) -> Optional[ActorMessage]:
        """Wait for a message in the inbox."""
        try:
            msg = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            logger.warning(f"Actor {self.id} timed out waiting for reply")
            return None

    def terminate(self, result: Optional[str] = None):
        """Terminate this actor."""
        if self.state == ActorState.TERMINATED:
            return  # Already terminated
        self._result = result or f"Actor {self.config.name} terminated"
        if self.task_state != TaskState.DONE:
            self.task_state = TaskState.DONE
        self.state = ActorState.TERMINATED
        self.terminated_at = datetime.now(timezone.utc)
        # Cancel async task if running
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info(f"Actor terminated: {self.config.name} (id={self.id}), result: {self._result[:80]}...")
        self.registry._on_actor_terminated(self.id)

    def kill_child(self, child_id: str) -> bool:
        """Kill an immediate child actor. Only parents can do this.
        
        Returns True if killed, False if not a child or already terminated.
        """
        child = self.registry.get(child_id)
        if child is None:
            return False
        if child.spawned_by != self.id:
            logger.warning(f"Actor {self.id} tried to kill non-child {child_id}")
            return False
        if child.state == ActorState.TERMINATED:
            return False
        child.terminate(f"Killed by parent {self.config.name}")
        return True

    def build_system_prompt(self) -> str:
        """Build the system prompt for this actor's LLM calls."""
        parts = []
        
        if self.is_principal:
            parts.extend(PRINCIPAL_PROMPT_BLOCK.splitlines())
        else:
            parent = self.registry.get(self.spawned_by)
            parent_name = parent.config.name if parent else self.spawned_by
            parts.extend(
                SUBAGENT_PROMPT_BLOCK.format(
                    actor_name=self.config.name,
                    parent_name=parent_name,
                    parent_id=self.spawned_by,
                ).splitlines()
            )
        
        parts.append(f"\n<goals>\n{self.config.goals}\n</goals>")
        
        # Group awareness — show all visible actors
        limits = self._PROMPT_LIMITS["principal" if self.is_principal else "subagent"]
        group_actors = self.registry.discover_active(self.config.group)
        children = self.registry.get_children(self.id)
        
        # Combine group + children (dedup by id)
        seen_ids = set()
        visible = []
        for info in group_actors:
            if info.id != self.id:
                visible.append(info)
                seen_ids.add(info.id)
        for child in children:
            if child.id not in seen_ids:
                visible.append(child.info)
                seen_ids.add(child.id)
        
        visible_sorted = sorted(
            visible,
            key=lambda info: (
                info.state == ActorState.TERMINATED,
                info.name,
                info.id,
            ),
        )
        omitted_visible = max(0, len(visible_sorted) - limits["visible_actors"])
        visible_limited = visible_sorted[:limits["visible_actors"]]

        if visible_limited:
            parts.append("\n<visible_actors>")
            for info in visible_limited:
                relationship = ""
                if info.spawned_by == self.id:
                    relationship = " [child]"
                elif info.id == self.spawned_by:
                    relationship = " [parent]"
                elif info.spawned_by == self.spawned_by and self.spawned_by:
                    relationship = " [sibling]"
                goal = info.goals[:limits["goal_chars"]]
                if len(info.goals) > limits["goal_chars"]:
                    goal += "...[truncated]"
                parts.append(
                    f"- {info.name} (id={info.id}, state={info.state.value}, task={info.task_state.value})"
                    f"{relationship}: {goal}"
                )
            if omitted_visible:
                parts.append(f"- [... {omitted_visible} more active actors omitted to save context ...]")
            parts.append("</visible_actors>")
        
        # Recent messages from other actors
        all_inbox_messages = [m for m in self._messages if m.sender != self.id]
        inbox_messages = all_inbox_messages[-limits["inbox_messages"]:]
        omitted_inbox = max(0, len(all_inbox_messages) - len(inbox_messages))
        if inbox_messages:
            parts.append("\n<inbox_block>")
            for m in inbox_messages:
                sender = self.registry.get(m.sender)
                sender_name = sender.config.name if sender else m.sender
                dt = m.created_at
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.astimezone(timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")
                content = m.content[:limits["content_chars"]]
                if len(m.content) > limits["content_chars"]:
                    content += "...[truncated]"
                parts.append(
                    f'<actor_message_block from="{sender_name}" timestamp="{ts}">{content}</actor_message_block>'
                )
            if omitted_inbox:
                parts.append(f"<inbox_omitted count=\"{omitted_inbox}\" />")
            parts.append("</inbox_block>")
        
        parts.append("\n<rules>")
        if self.is_principal:
            parts.extend(PRINCIPAL_RULES_BLOCK.splitlines())
        else:
            parts.extend(
                SUBAGENT_RULES_BLOCK.format(
                    parent_name=parent_name,
                    parent_id=self.spawned_by,
                ).splitlines()
            )
        parts.append("</rules>")

        self._last_prompt_stats = {
            "visible_total": len(visible),
            "visible_included": len(visible_limited),
            "inbox_total": len(all_inbox_messages),
            "inbox_included": len(inbox_messages),
        }
        return "\n".join(parts)

    def get_context_messages(self) -> List[Dict]:
        """Get conversation-formatted messages for LLM context."""
        result = []
        for msg in self._messages[-self.config.max_messages:]:
            if msg.sender == self.id:
                result.append({"role": "assistant", "content": msg.content})
            else:
                actor = self.registry.get(msg.sender)
                label = actor.config.name if actor else msg.sender
                result.append({"role": "user", "content": f"[From {label}]: {msg.content}"})
        return result


class ActorRegistry:
    """Central registry for all actors. Manages lifecycle and discovery."""

    def __init__(self):
        self._actors: Dict[str, Actor] = {}
        self._principal_id: Optional[str] = None
        # Name → ID index for duplicate detection
        self._name_index: Dict[str, str] = {}
        self.events = ActorEventBus()
        # Callbacks
        self._on_user_message: Optional[Callable] = None
        self._llm_factory: Optional[Callable] = None

    def emit_event(self, event_type: str, actor: Actor, payload: Optional[Dict[str, Any]] = None):
        """Emit structured actor event."""
        self.events.emit(
            ActorEvent(
                event_type=event_type,
                actor_id=actor.id,
                group=actor.config.group,
                payload=payload or {},
            )
        )

    def set_llm_factory(self, factory: Callable):
        """Set factory function that creates LLM clients for actors.
        
        Args:
            factory: async Callable(actor: Actor) -> AsyncLLMClient
        """
        self._llm_factory = factory

    def set_user_callback(self, callback: Callable):
        """Set callback for when the principal actor sends messages to the user.
        
        Args:
            callback: async Callable(message: str) -> None
        """
        self._on_user_message = callback

    def find_by_name(self, name: str, group: str = "") -> Optional[Actor]:
        """Find a running actor by name (and optionally group).
        
        Used to check if an actor already exists before spawning a duplicate.
        """
        for actor in self._actors.values():
            if actor.config.name == name and actor.state != ActorState.TERMINATED:
                if not group or actor.config.group == group:
                    return actor
        return None

    def spawn(
        self,
        config: ActorConfig,
        spawned_by: Optional[str] = None,
        is_principal: bool = False,
    ) -> Actor:
        """Spawn a new actor.
        
        Args:
            config: Actor configuration
            spawned_by: ID of the actor that spawned this one
            is_principal: Whether this is the principal (user-facing) actor
            
        Returns:
            The newly created Actor
        """
        actor = Actor(
            config=config,
            registry=self,
            spawned_by=spawned_by,
            is_principal=is_principal,
        )
        self._actors[actor.id] = actor
        self._name_index[config.name] = actor.id
        
        if is_principal:
            self._principal_id = actor.id
        
        actor.state = ActorState.RUNNING
        actor.task_state = TaskState.RUNNING
        self.emit_event(
            "actor_spawned",
            actor,
            {"name": actor.config.name, "spawned_by": actor.spawned_by, "is_principal": is_principal},
        )
        logger.info(f"Registry: spawned {actor.config.name} (id={actor.id}, principal={is_principal})")
        return actor

    def get(self, actor_id: str) -> Optional[Actor]:
        """Get an actor by ID."""
        return self._actors.get(actor_id)

    def get_principal(self) -> Optional[Actor]:
        """Get the principal (user-facing) actor."""
        if self._principal_id:
            return self._actors.get(self._principal_id)
        return None

    def discover(self, group: str) -> List[ActorInfo]:
        """Discover all actors in a group (including recently terminated)."""
        return [
            actor.info
            for actor in self._actors.values()
            if actor.config.group == group
        ]

    def discover_active(self, group: str) -> List[ActorInfo]:
        """Discover active (non-terminated) actors in a group."""
        return [
            actor.info
            for actor in self._actors.values()
            if actor.config.group == group and actor.state != ActorState.TERMINATED
        ]

    def discover_terminated(self, group: str) -> List[ActorInfo]:
        """Discover terminated actors in a group."""
        return [
            actor.info
            for actor in self._actors.values()
            if actor.config.group == group and actor.state == ActorState.TERMINATED
        ]

    def discover_recently_finished(self, group: str, limit: int = 5) -> List[Actor]:
        """Return recently terminated actors in a group, newest first."""
        terminated = [
            actor for actor in self._actors.values()
            if actor.config.group == group and actor.state == ActorState.TERMINATED
        ]
        terminated.sort(key=lambda a: a.terminated_at or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True)
        return terminated[:max(1, limit)]

    def get_children(self, parent_id: str) -> List[Actor]:
        """Get all actors spawned by a given parent (including recently terminated)."""
        return [
            actor for actor in self._actors.values()
            if actor.spawned_by == parent_id
        ]

    def _on_actor_terminated(self, actor_id: str):
        """Called when an actor terminates."""
        actor = self._actors.get(actor_id)
        if not actor:
            return
        
        result_text = (actor._result or "no result").strip()
        lowered = result_text.lower()
        is_failed = (
            lowered.startswith("error:")
            or lowered.startswith("runner error:")
            or lowered.startswith("max turns reached")
            or "killed by parent" in lowered
            or lowered.startswith("system shutdown")
        )
        status_kind = "failed" if is_failed else "done"
        
        # Notify parent if exists and running
        parent = self._actors.get(actor.spawned_by) if actor.spawned_by else None
        if parent and parent.state == ActorState.RUNNING:
            msg = ActorMessage(
                sender=actor_id,
                recipient=actor.spawned_by,
                content=f"{actor.config.name}: {result_text}",
                metadata={
                    "channel": "task_update",
                    "kind": status_kind,
                    "source": "termination",
                },
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(parent.send(msg))
            except RuntimeError:
                parent._messages.append(msg)
                parent._inbox.put_nowait(msg)
        self.emit_event(
            "actor_terminated",
            actor,
            {"name": actor.config.name, "result": actor._result or "", "turns": actor._turns},
        )

    @property
    def active_count(self) -> int:
        """Number of non-terminated actors."""
        return sum(1 for a in self._actors.values() if a.state != ActorState.TERMINATED)

    @property
    def all_actors(self) -> List[ActorInfo]:
        """Info for all actors (including terminated)."""
        return [a.info for a in self._actors.values()]

    # Stale threshold — terminated actors kept for querying results
    STALE_SECONDS = 3600  # 1 hour

    def cleanup_terminated(self, force: bool = False):
        """Remove stale terminated actors from registry.
        
        Args:
            force: If True, remove ALL terminated actors (used on shutdown).
                   If False, only remove actors terminated > STALE_SECONDS ago.
        """
        now = datetime.now(timezone.utc)
        stale = []
        for aid, a in self._actors.items():
            if a.state != ActorState.TERMINATED:
                continue
            if force:
                stale.append(aid)
            elif a.terminated_at and (now - a.terminated_at).total_seconds() > self.STALE_SECONDS:
                stale.append(aid)
        
        for aid in stale:
            actor = self._actors.pop(aid)
            # Clean name index
            if self._name_index.get(actor.config.name) == aid:
                del self._name_index[actor.config.name]
        if stale:
            logger.info(f"Registry: cleaned up {len(stale)} stale actors (>{self.STALE_SECONDS}s)")
