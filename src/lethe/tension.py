"""Tension registry — unresolved business that drives initiative.

The entity doesn't act because a timer went off. It acts because something
is unfinished and the unfinished-ness has accumulated past a threshold.

The DMN maintains the registry during consolidation. Each cycle it:
- Identifies new unresolved items (questions, promises, incomplete work)
- Updates scores on existing items (tension grows with time)
- Resolves items that have been addressed
- Surfaces high-tension items to drives/cognition

Tension categories:
- unanswered_question: A question that keeps coming back
- incomplete_work: A project or task left unfinished
- unfulfilled_promise: Something committed to but not done
- unprocessed_experience: Emotionally significant events not yet integrated
- pattern_mismatch: Something that doesn't make sense yet
- value_conflict: A constitutional tension being tested by circumstances
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Tension threshold — above this, the entity should act
DEFAULT_THRESHOLD = 0.6

# How much tension grows per hour of inaction
TENSION_GROWTH_PER_HOUR = 0.02

# Maximum tension score
MAX_TENSION = 1.0


@dataclass
class TensionItem:
    """An unresolved item creating internal tension."""
    item: str                   # Description of the unresolved thing
    score: float = 0.3          # Current tension score [0, 1]
    category: str = ""          # Category (see module docstring)
    created_at: str = ""        # ISO timestamp
    last_updated: str = ""      # ISO timestamp
    related_user: str = ""      # User ID if person-related
    resolved: bool = False


class TensionRegistry:
    """Manages unresolved items that drive initiative."""

    def __init__(self, workspace_dir: str, threshold: float = DEFAULT_THRESHOLD):
        self._path = os.path.join(workspace_dir, "tension_registry.json")
        self._threshold = threshold
        self._items: list[TensionItem] = []
        self._load()

    def _load(self):
        """Load registry from disk."""
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            self._items = [TensionItem(**item) for item in data if not item.get("resolved")]
            logger.info("Loaded %d tension items", len(self._items))
        except Exception as e:
            logger.warning("Failed to load tension registry: %s", e)

    def save(self):
        """Persist registry to disk."""
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            # Save unresolved items only
            data = [asdict(item) for item in self._items if not item.resolved]
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save tension registry: %s", e)

    def add(self, item: str, category: str = "", score: float = 0.3, related_user: str = "") -> TensionItem:
        """Add a new unresolved item."""
        # Check for duplicates (fuzzy)
        item_lower = item.lower().strip()
        for existing in self._items:
            if not existing.resolved and existing.item.lower().strip() == item_lower:
                # Boost existing instead of duplicating
                existing.score = min(MAX_TENSION, existing.score + 0.1)
                existing.last_updated = datetime.now(timezone.utc).isoformat()
                return existing

        now = datetime.now(timezone.utc).isoformat()
        tension = TensionItem(
            item=item,
            score=min(MAX_TENSION, score),
            category=category,
            created_at=now,
            last_updated=now,
            related_user=related_user,
        )
        self._items.append(tension)
        self.save()
        logger.info("Tension added: %.2f %s — %s", score, category, item[:60])
        return tension

    def resolve(self, item_text: str):
        """Mark an item as resolved (removes tension)."""
        item_lower = item_text.lower().strip()
        for item in self._items:
            if not item.resolved and item.item.lower().strip() == item_lower:
                item.resolved = True
                item.last_updated = datetime.now(timezone.utc).isoformat()
                logger.info("Tension resolved: %s", item_text[:60])
        self.save()

    def tick(self, elapsed_hours: float = 1.0):
        """Grow tension scores over time. Called during DMN consolidation."""
        for item in self._items:
            if item.resolved:
                continue
            item.score = min(MAX_TENSION, item.score + TENSION_GROWTH_PER_HOUR * elapsed_hours)
            item.last_updated = datetime.now(timezone.utc).isoformat()
        self.save()

    def get_above_threshold(self) -> list[TensionItem]:
        """Get items with tension above the action threshold."""
        return [
            item for item in self._items
            if not item.resolved and item.score >= self._threshold
        ]

    def get_all_active(self) -> list[TensionItem]:
        """Get all unresolved items, sorted by score descending."""
        active = [item for item in self._items if not item.resolved]
        active.sort(key=lambda x: x.score, reverse=True)
        return active

    def get_summary(self) -> str:
        """Human-readable summary for LLM context."""
        active = self.get_all_active()
        if not active:
            return "No unresolved tensions."

        above = [i for i in active if i.score >= self._threshold]
        below = [i for i in active if i.score < self._threshold]

        lines = []
        if above:
            lines.append(f"Unresolved tensions above threshold ({len(above)}):")
            for item in above[:5]:
                lines.append(f"  [{item.score:.2f}] {item.item[:80]} ({item.category})")
        if below:
            lines.append(f"Background tensions ({len(below)}):")
            for item in below[:5]:
                lines.append(f"  [{item.score:.2f}] {item.item[:80]}")

        return "\n".join(lines)

    def get_json_for_dmn(self) -> str:
        """JSON representation for DMN to read/update."""
        active = self.get_all_active()
        return json.dumps([asdict(item) for item in active[:20]], indent=2)
