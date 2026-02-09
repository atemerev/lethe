"""Actor model for Lethe — subagents with lifecycles.

Actors are autonomous agents that can:
- Have their own goals, model, and tools
- Discover other actors in their group
- Communicate with parents, siblings, and children
- Spawn child actors for subtasks
- Terminate themselves or their immediate children

The principal actor ("butler") is the only one that talks to the user.
All other actors communicate through the principal or with each other.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ActorState(str, Enum):
    """Lifecycle states for an actor."""
    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING = "waiting"  # Waiting for response from another actor
    TERMINATED = "terminated"


@dataclass
class ActorMessage:
    """Message passed between actors."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    sender: str = ""       # Actor ID of sender
    recipient: str = ""    # Actor ID of recipient
    content: str = ""      # Message text
    reply_to: Optional[str] = None  # Message ID this replies to
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def format(self) -> str:
        """Format for inclusion in actor context."""
        ts = self.created_at.strftime("%H:%M:%S")
        reply = f" (reply to {self.reply_to})" if self.reply_to else ""
        return f"[{ts}] {self.sender}{reply}: {self.content}"


@dataclass
class ActorConfig:
    """Configuration for spawning an actor."""
    name: str                              # Human-readable name (e.g., "researcher")
    group: str = "default"                 # Actor group for discovery
    goals: str = ""                        # What this actor should accomplish
    model: str = ""                        # LLM model override (empty = use aux)
    tools: List[str] = field(default_factory=list)  # Tool names available to this actor
    max_turns: int = 20                    # Max LLM turns before forced termination
    max_messages: int = 50                 # Max inter-actor messages


@dataclass
class ActorInfo:
    """Public information about an actor, visible to other actors in the group."""
    id: str
    name: str
    group: str
    goals: str
    state: ActorState
    spawned_by: str  # Actor ID that created this one

    def format(self) -> str:
        """Format for inclusion in actor context."""
        return f"- {self.name} (id={self.id}, state={self.state.value}): {self.goals}"


class Actor:
    """An autonomous agent with a lifecycle.
    
    Each actor has its own LLM client, tools, goals, and message queue.
    The principal actor is special — it receives user messages and sends
    responses back to the user.
    """

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
            spawned_by=self.spawned_by,
        )

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

    async def send_to(self, recipient_id: str, content: str, reply_to: Optional[str] = None) -> ActorMessage:
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
        )
        await recipient.send(msg)
        self._messages.append(msg)
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
        self.state = ActorState.TERMINATED
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
            parts.append("You are the principal actor (butler) — the user's direct assistant.")
            parts.append("You are the ONLY actor that communicates with the user.")
            parts.append("You can spawn subagents to handle subtasks, then report results to the user.")
        else:
            parent = self.registry.get(self.spawned_by)
            parent_name = parent.config.name if parent else self.spawned_by
            parts.append(f"You are a subagent actor named '{self.config.name}'.")
            parts.append(f"You were spawned by '{parent_name}' (id={self.spawned_by}) to accomplish a specific task.")
            parts.append("You CANNOT talk to the user directly. Report your results to the actor that spawned you.")
        
        parts.append(f"\n<goals>\n{self.config.goals}\n</goals>")
        
        # Group awareness — show all visible actors
        group_actors = self.registry.discover(self.config.group)
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
        
        if visible:
            parts.append("\n<visible_actors>")
            for info in visible:
                relationship = ""
                if info.spawned_by == self.id:
                    relationship = " [child]"
                elif info.id == self.spawned_by:
                    relationship = " [parent]"
                elif info.spawned_by == self.spawned_by and self.spawned_by:
                    relationship = " [sibling]"
                parts.append(f"- {info.name} (id={info.id}, state={info.state.value}){relationship}: {info.goals}")
            parts.append("</visible_actors>")
        
        # Recent messages from other actors
        inbox_messages = [m for m in self._messages if m.sender != self.id][-10:]
        if inbox_messages:
            parts.append("\n<inbox>")
            parts.append("Recent messages from other actors:")
            for m in inbox_messages:
                sender = self.registry.get(m.sender)
                sender_name = sender.config.name if sender else m.sender
                ts = m.created_at.strftime("%H:%M:%S")
                parts.append(f"[{ts}] {sender_name}: {m.content}")
            parts.append("</inbox>")
        
        parts.append("\n<rules>")
        parts.append("- Use `send_message(actor_id, content)` to message parents, siblings, children, or group members")
        parts.append("- Use `discover_actors(group)` to find actors in a group")
        if self.is_principal:
            parts.append("- Use `spawn_subagent(name, goals, ...)` to create child actors for subtasks")
            parts.append("- Use `kill_actor(actor_id)` to terminate an immediate child actor")
        parts.append("- Use `terminate(result)` when YOUR task is complete — include a summary of results")
        if not self.is_principal:
            parts.append(f"- Report results to your parent '{parent_name}' (id={self.spawned_by}) before terminating")
        parts.append("</rules>")
        
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
        # Callbacks
        self._on_user_message: Optional[Callable] = None
        self._llm_factory: Optional[Callable] = None

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
        """Discover all non-terminated actors in a group."""
        return [
            actor.info
            for actor in self._actors.values()
            if actor.config.group == group and actor.state != ActorState.TERMINATED
        ]

    def get_children(self, parent_id: str) -> List[Actor]:
        """Get all non-terminated actors spawned by a given parent."""
        return [
            actor for actor in self._actors.values()
            if actor.spawned_by == parent_id and actor.state != ActorState.TERMINATED
        ]

    def _on_actor_terminated(self, actor_id: str):
        """Called when an actor terminates."""
        actor = self._actors.get(actor_id)
        if not actor:
            return
        
        # Notify parent if exists and running
        parent = self._actors.get(actor.spawned_by) if actor.spawned_by else None
        if parent and parent.state == ActorState.RUNNING:
            msg = ActorMessage(
                sender=actor_id,
                recipient=actor.spawned_by,
                content=f"[TERMINATED] {actor.config.name} finished: {actor._result or 'no result'}",
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(parent.send(msg))
            except RuntimeError:
                parent._messages.append(msg)
                parent._inbox.put_nowait(msg)

    @property
    def active_count(self) -> int:
        """Number of non-terminated actors."""
        return sum(1 for a in self._actors.values() if a.state != ActorState.TERMINATED)

    @property
    def all_actors(self) -> List[ActorInfo]:
        """Info for all actors (including terminated)."""
        return [a.info for a in self._actors.values()]

    def cleanup_terminated(self):
        """Remove terminated actors from registry."""
        terminated = [aid for aid, a in self._actors.items() if a.state == ActorState.TERMINATED]
        for aid in terminated:
            actor = self._actors.pop(aid)
            # Clean name index
            if self._name_index.get(actor.config.name) == aid:
                del self._name_index[actor.config.name]
        if terminated:
            logger.info(f"Registry: cleaned up {len(terminated)} terminated actors")
