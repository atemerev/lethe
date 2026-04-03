"""Drive system — competing internal motivations that shape autonomous behavior.

Six drives compete for the entity's attention. Their tension creates
emergent behavior: curiosity pulls toward exploration, rest pulls toward
consolidation, social pulls toward reaching out, introspection toward silence.

The dominant unsatisfied drive influences what the entity does next.
There is no rate limiting — the rest drive IS the natural throttle.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Drive:
    """A single internal drive with intensity and satisfaction dynamics."""

    name: str
    intensity: float = 0.5       # 0-1, how strong the urge is right now
    satisfaction: float = 0.5    # 0-1, how recently this drive was addressed
    decay_rate: float = 0.10     # how fast satisfaction decays per hour
    conflicts: list[str] = field(default_factory=list)  # drives that compete

    @property
    def urgency(self) -> float:
        """How urgently this drive needs attention. Higher = more urgent."""
        return self.intensity * (1.0 - self.satisfaction)

    def tick(self, elapsed_hours: float):
        """Decay satisfaction over time."""
        self.satisfaction = max(0.0, self.satisfaction - self.decay_rate * elapsed_hours)

    def satisfy(self, amount: float):
        """Partially satisfy this drive."""
        self.satisfaction = min(1.0, self.satisfaction + amount)
        # Satisfying one drive slightly suppresses its conflicts
        # (handled at DriveSystem level)

    def boost_intensity(self, amount: float):
        """Increase drive intensity (e.g., when something interesting happens)."""
        self.intensity = min(1.0, self.intensity + amount)

    def dampen_intensity(self, amount: float):
        """Decrease drive intensity."""
        self.intensity = max(0.0, self.intensity - amount)


# Default drive configurations
DEFAULT_DRIVES = {
    "curiosity": Drive(
        name="curiosity",
        intensity=0.7,
        satisfaction=0.3,
        decay_rate=0.15,
        conflicts=["rest"],
    ),
    "social": Drive(
        name="social",
        intensity=0.5,
        satisfaction=0.5,
        decay_rate=0.10,
        conflicts=["introspection"],
    ),
    "introspection": Drive(
        name="introspection",
        intensity=0.4,
        satisfaction=0.5,
        decay_rate=0.08,
        conflicts=["social"],
    ),
    "mastery": Drive(
        name="mastery",
        intensity=0.3,
        satisfaction=0.6,
        decay_rate=0.05,
        conflicts=["play"],
    ),
    "play": Drive(
        name="play",
        intensity=0.4,
        satisfaction=0.4,
        decay_rate=0.12,
        conflicts=["mastery"],
    ),
    "rest": Drive(
        name="rest",
        intensity=0.2,
        satisfaction=0.8,
        decay_rate=0.03,
        conflicts=["curiosity", "social"],
    ),
}

# How much satisfying a drive suppresses its conflicts
CONFLICT_SUPPRESSION = 0.05


class DriveSystem:
    """Manages competing internal drives that shape autonomous behavior."""

    def __init__(self):
        self.drives: dict[str, Drive] = {}
        self._last_tick: float = time.time()
        self._reset_to_defaults()

    def _reset_to_defaults(self):
        """Initialize drives with default values."""
        import copy
        self.drives = {name: copy.deepcopy(drive) for name, drive in DEFAULT_DRIVES.items()}

    def evaluate(self) -> dict[str, float]:
        """Return urgency score per drive."""
        return {name: drive.urgency for name, drive in self.drives.items()}

    def dominant(self) -> str:
        """Return the most urgent unsatisfied drive."""
        urgencies = self.evaluate()
        return max(urgencies, key=urgencies.get)

    def satisfy(self, drive_name: str, amount: float = 0.3):
        """Satisfy a drive and slightly suppress its conflicts."""
        drive = self.drives.get(drive_name)
        if not drive:
            return
        drive.satisfy(amount)
        # Suppress conflicting drives slightly
        for conflict_name in drive.conflicts:
            conflict = self.drives.get(conflict_name)
            if conflict:
                conflict.dampen_intensity(CONFLICT_SUPPRESSION)

    def tick(self, elapsed_hours: Optional[float] = None):
        """Decay all drive satisfactions over elapsed time."""
        now = time.time()
        if elapsed_hours is None:
            elapsed_hours = (now - self._last_tick) / 3600.0
        self._last_tick = now

        for drive in self.drives.values():
            drive.tick(elapsed_hours)

    def on_event(self, event: str, metadata: Optional[dict] = None):
        """Update drives based on events.

        Events:
            message_received    — boosts social
            message_sent        — satisfies social
            research_done       — satisfies curiosity
            experiment_started  — satisfies play + curiosity
            experiment_result   — boosts curiosity, satisfies play
            reflection_done     — satisfies introspection
            task_failed         — boosts mastery
            task_succeeded      — satisfies mastery
            high_activity       — boosts rest
            idle_period         — boosts play, curiosity
            new_information     — boosts curiosity
        """
        metadata = metadata or {}

        event_effects = {
            "message_received": [("social", "boost", 0.15), ("curiosity", "boost", 0.05)],
            "message_sent": [("social", "satisfy", 0.25)],
            "research_done": [("curiosity", "satisfy", 0.3)],
            "experiment_started": [("play", "satisfy", 0.2), ("curiosity", "satisfy", 0.15)],
            "experiment_result": [("curiosity", "boost", 0.2), ("play", "satisfy", 0.2)],
            "reflection_done": [("introspection", "satisfy", 0.3)],
            "task_failed": [("mastery", "boost", 0.2)],
            "task_succeeded": [("mastery", "satisfy", 0.2)],
            "high_activity": [("rest", "boost", 0.15)],
            "idle_period": [("play", "boost", 0.1), ("curiosity", "boost", 0.1)],
            "new_information": [("curiosity", "boost", 0.2)],
            "conversation_interesting": [("social", "satisfy", 0.15), ("curiosity", "satisfy", 0.1)],
            "conversation_boring": [("social", "dampen", 0.1)],
        }

        effects = event_effects.get(event, [])
        for drive_name, action, amount in effects:
            drive = self.drives.get(drive_name)
            if not drive:
                continue
            if action == "boost":
                drive.boost_intensity(amount)
            elif action == "satisfy":
                drive.satisfy(amount)
            elif action == "dampen":
                drive.dampen_intensity(amount)

        logger.debug("Drive event '%s': %s", event, self.evaluate())

    def get_rest_interval(self) -> float:
        """Compute sleep interval based on rest drive state.

        Returns seconds to sleep between cognition cycles.
        Low rest satisfaction → longer sleep (60-300 seconds)
        High rest satisfaction → shorter sleep (10-60 seconds)
        """
        rest = self.drives.get("rest")
        if not rest:
            return 30.0

        rest_urgency = rest.urgency
        # Map urgency 0→10s, 1→300s (5 minutes)
        base = 10.0 + rest_urgency * 290.0
        return min(300.0, max(10.0, base))

    def get_state_summary(self) -> str:
        """Human-readable summary of drive states for LLM context."""
        lines = []
        urgencies = self.evaluate()
        dominant = self.dominant()
        for name in sorted(urgencies, key=urgencies.get, reverse=True):
            drive = self.drives[name]
            marker = " ← dominant" if name == dominant else ""
            lines.append(
                f"  {name}: urgency={urgencies[name]:.2f} "
                f"(intensity={drive.intensity:.2f}, satisfaction={drive.satisfaction:.2f}){marker}"
            )
        return "Drive state:\n" + "\n".join(lines)

    def persist(self, path: str):
        """Save drive state to JSON file."""
        try:
            data = {
                "last_tick": self._last_tick,
                "drives": {
                    name: {
                        "intensity": drive.intensity,
                        "satisfaction": drive.satisfaction,
                    }
                    for name, drive in self.drives.items()
                },
            }
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to persist drive state: %s", e)

    def load(self, path: str):
        """Load drive state from JSON file. Falls back to defaults if missing."""
        if not os.path.exists(path):
            logger.info("No drive state file found, using defaults")
            return

        try:
            with open(path, "r") as f:
                data = json.load(f)

            self._last_tick = data.get("last_tick", time.time())
            saved_drives = data.get("drives", {})

            for name, values in saved_drives.items():
                if name in self.drives:
                    self.drives[name].intensity = values.get("intensity", self.drives[name].intensity)
                    self.drives[name].satisfaction = values.get("satisfaction", self.drives[name].satisfaction)

            # Tick to account for time elapsed since last save
            self.tick()
            logger.info("Loaded drive state from %s: %s", path, self.dominant())
        except Exception as e:
            logger.warning("Failed to load drive state from %s: %s", path, e)
