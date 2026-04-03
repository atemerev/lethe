"""Relationship manager — multi-user with unified memory.

The entity knows multiple people. Memories are shared (like in humans) —
there's one memory store, not per-user partitions. The entity has the wisdom
not to gossip sensitive information between people, but it remembers everything
in one place. Per-person notes track the relationship itself.

Privacy is a social skill, not an architectural wall.
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Relationship:
    """A relationship with one person."""

    user_id: str                     # Stable identifier (e.g., telegram user ID)
    display_name: str                # How the entity knows them
    chat_id: int                     # Telegram chat ID for sending messages
    first_contact: str = ""          # ISO timestamp of first interaction
    last_interaction: str = ""       # ISO timestamp of last message (either direction)
    interaction_count: int = 0       # Total messages exchanged

    def base_dir(self, workspace_dir: str) -> str:
        """Directory for this person's relationship notes."""
        return os.path.join(workspace_dir, "relationships", self.user_id)

    def notes_path(self, workspace_dir: str) -> str:
        """Path to relationship notes (entity's thoughts about this person)."""
        return os.path.join(self.base_dir(workspace_dir), "relationship.md")

    def touch(self):
        """Update last interaction timestamp and count."""
        self.last_interaction = datetime.now(timezone.utc).isoformat()
        self.interaction_count += 1

    @property
    def hours_since_last_interaction(self) -> float:
        """Hours since last interaction. Returns infinity if never interacted."""
        if not self.last_interaction:
            return float("inf")
        try:
            last = datetime.fromisoformat(self.last_interaction)
            delta = datetime.now(timezone.utc) - last
            return delta.total_seconds() / 3600.0
        except Exception:
            return float("inf")


class RelationshipManager:
    """Manages all relationships. Memory is unified — not per-user separated.

    The entity uses one shared memory store (conversation history, archival).
    Messages are tagged with user_id for context but not isolated into
    separate tables. The entity relies on social wisdom (not architectural
    walls) to avoid gossiping.

    Per-person files track relationship notes only — not memories.
    """

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir
        self._relationships: dict[str, Relationship] = {}
        self._active_user_id: Optional[str] = None
        self._registry_path = os.path.join(workspace_dir, "relationships", "registry.json")
        self._load_registry()

    def _load_registry(self):
        """Load relationship registry from disk."""
        if not os.path.exists(self._registry_path):
            return
        try:
            with open(self._registry_path, "r") as f:
                data = json.load(f)
            for user_id, info in data.items():
                self._relationships[user_id] = Relationship(
                    user_id=user_id,
                    display_name=info.get("display_name", ""),
                    chat_id=info.get("chat_id", 0),
                    first_contact=info.get("first_contact", ""),
                    last_interaction=info.get("last_interaction", ""),
                    interaction_count=info.get("interaction_count", 0),
                )
            logger.info("Loaded %d relationships from registry", len(self._relationships))
        except Exception as e:
            logger.warning("Failed to load relationship registry: %s", e)

    def _save_registry(self):
        """Save relationship registry to disk."""
        try:
            os.makedirs(os.path.dirname(self._registry_path), exist_ok=True)
            data = {}
            for user_id, rel in self._relationships.items():
                data[user_id] = {
                    "display_name": rel.display_name,
                    "chat_id": rel.chat_id,
                    "first_contact": rel.first_contact,
                    "last_interaction": rel.last_interaction,
                    "interaction_count": rel.interaction_count,
                }
            with open(self._registry_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save relationship registry: %s", e)

    def get_or_create(self, user_id: str, chat_id: int, display_name: str = "") -> Relationship:
        """Get existing relationship or create a new one."""
        if user_id in self._relationships:
            rel = self._relationships[user_id]
            if chat_id:
                rel.chat_id = chat_id
            if display_name and display_name != rel.display_name:
                rel.display_name = display_name
            return rel

        now = datetime.now(timezone.utc).isoformat()
        rel = Relationship(
            user_id=user_id,
            display_name=display_name or f"user_{user_id}",
            chat_id=chat_id,
            first_contact=now,
        )
        self._relationships[user_id] = rel

        # Create relationship notes directory
        base_dir = rel.base_dir(self._workspace_dir)
        os.makedirs(base_dir, exist_ok=True)

        notes_path = rel.notes_path(self._workspace_dir)
        if not os.path.exists(notes_path):
            with open(notes_path, "w") as f:
                f.write(f"# Relationship: {rel.display_name}\n\nFirst contact: {now}\n")

        self._save_registry()
        logger.info("Created new relationship: %s (%s)", display_name, user_id)
        return rel

    def get(self, user_id: str) -> Optional[Relationship]:
        """Get a relationship by user_id, or None if unknown."""
        return self._relationships.get(user_id)

    def get_all(self) -> list[Relationship]:
        """Get all known relationships."""
        return list(self._relationships.values())

    def get_active_context(self) -> Optional[Relationship]:
        """Get the currently-active dialog's relationship."""
        if self._active_user_id:
            return self._relationships.get(self._active_user_id)
        return None

    def set_active(self, user_id: str):
        """Set which relationship is currently active (for dialog context)."""
        self._active_user_id = user_id

    def clear_active(self):
        """Clear the active relationship context."""
        self._active_user_id = None

    def get_candidates_for_social(self, max_count: int = 5) -> list[Relationship]:
        """Return relationships the entity might want to reach out to.

        Sorted by: time since last interaction weighted by familiarity.
        """
        candidates = []
        for rel in self._relationships.values():
            hours = rel.hours_since_last_interaction
            if hours < 1.0:
                continue
            score = hours * math.log(rel.interaction_count + 2)
            candidates.append((score, rel))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [rel for _, rel in candidates[:max_count]]

    def record_interaction(self, user_id: str):
        """Record that an interaction happened with this user."""
        rel = self._relationships.get(user_id)
        if rel:
            rel.touch()
            self._save_registry()

    def get_summary(self) -> str:
        """Human-readable summary of all relationships for LLM context."""
        if not self._relationships:
            return "No relationships yet."
        lines = ["People you know:"]
        for rel in sorted(self._relationships.values(), key=lambda r: r.last_interaction or "", reverse=True):
            hours = rel.hours_since_last_interaction
            if hours < 1:
                last = "just now"
            elif hours < 24:
                last = f"{hours:.0f}h ago"
            elif hours < 168:
                last = f"{hours/24:.0f}d ago"
            else:
                last = f"{hours/168:.0f}w ago"
            lines.append(f"  - {rel.display_name} (last: {last}, {rel.interaction_count} messages)")
        return "\n".join(lines)
