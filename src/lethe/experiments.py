"""Experiment system — self-directed investigation driven by curiosity.

The entity can formulate hypotheses, design methods to test them,
execute experiments across multiple cognition cycles, and learn from results.
Experiments can be about anything: code, ideas, communication patterns,
web research, creative projects.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExperimentStep:
    """A single step in an experiment."""
    description: str
    result: str = ""
    timestamp: str = ""
    success: Optional[bool] = None


@dataclass
class Experiment:
    """A self-directed experiment."""

    id: str = ""
    hypothesis: str = ""           # What the entity thinks might be true
    method: str = ""               # How to test it
    status: str = "proposed"       # "proposed", "running", "concluded", "abandoned"
    steps: list[dict] = field(default_factory=list)
    conclusion: str = ""           # What was learned
    drive_source: str = ""         # Which drive motivated this
    created_at: str = ""
    concluded_at: str = ""
    tags: list[str] = field(default_factory=list)


class ExperimentRunner:
    """Manages the entity's self-directed experiments."""

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir
        self._experiments_dir = os.path.join(workspace_dir, "experiments")
        os.makedirs(self._experiments_dir, exist_ok=True)

    def propose(self, hypothesis: str, method: str, drive: str = "curiosity", tags: Optional[list[str]] = None) -> Experiment:
        """Propose a new experiment."""
        exp = Experiment(
            id=str(uuid.uuid4())[:8],
            hypothesis=hypothesis,
            method=method,
            status="proposed",
            drive_source=drive,
            created_at=datetime.now(timezone.utc).isoformat(),
            tags=tags or [],
        )
        self._save(exp)
        logger.info("Experiment proposed: %s — %s", exp.id, hypothesis[:80])
        return exp

    def start(self, experiment_id: str) -> Optional[Experiment]:
        """Mark an experiment as running."""
        exp = self._load(experiment_id)
        if not exp:
            return None
        exp.status = "running"
        self._save(exp)
        return exp

    def add_step(self, experiment_id: str, description: str, result: str = "", success: Optional[bool] = None) -> Optional[Experiment]:
        """Add a step to a running experiment."""
        exp = self._load(experiment_id)
        if not exp:
            return None
        step = {
            "description": description,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": success,
        }
        exp.steps.append(step)
        self._save(exp)
        return exp

    def conclude(self, experiment_id: str, conclusion: str) -> Optional[Experiment]:
        """Conclude an experiment with learnings."""
        exp = self._load(experiment_id)
        if not exp:
            return None
        exp.status = "concluded"
        exp.conclusion = conclusion
        exp.concluded_at = datetime.now(timezone.utc).isoformat()
        self._save(exp)
        logger.info("Experiment concluded: %s — %s", exp.id, conclusion[:80])
        return exp

    def abandon(self, experiment_id: str, reason: str = "") -> Optional[Experiment]:
        """Abandon an experiment."""
        exp = self._load(experiment_id)
        if not exp:
            return None
        exp.status = "abandoned"
        exp.conclusion = f"Abandoned: {reason}" if reason else "Abandoned"
        exp.concluded_at = datetime.now(timezone.utc).isoformat()
        self._save(exp)
        return exp

    def get_active(self) -> list[Experiment]:
        """Get all proposed or running experiments."""
        experiments = []
        for filename in os.listdir(self._experiments_dir):
            if not filename.endswith(".json"):
                continue
            exp = self._load(filename.replace(".json", ""))
            if exp and exp.status in ("proposed", "running"):
                experiments.append(exp)
        return sorted(experiments, key=lambda e: e.created_at, reverse=True)

    def get_history(self, limit: int = 20) -> list[Experiment]:
        """Get concluded/abandoned experiments."""
        experiments = []
        for filename in os.listdir(self._experiments_dir):
            if not filename.endswith(".json"):
                continue
            exp = self._load(filename.replace(".json", ""))
            if exp and exp.status in ("concluded", "abandoned"):
                experiments.append(exp)
        return sorted(experiments, key=lambda e: e.concluded_at or "", reverse=True)[:limit]

    def get_summary(self) -> str:
        """Summary of active experiments for LLM context."""
        active = self.get_active()
        if not active:
            return "No active experiments."
        lines = ["Active experiments:"]
        for exp in active[:5]:
            step_count = len(exp.steps)
            lines.append(f"  - [{exp.status}] {exp.hypothesis[:80]} ({step_count} steps, drive: {exp.drive_source})")
        return "\n".join(lines)

    def _save(self, exp: Experiment):
        """Save experiment to file."""
        path = os.path.join(self._experiments_dir, f"{exp.id}.json")
        try:
            with open(path, "w") as f:
                json.dump(asdict(exp), f, indent=2)
        except Exception as e:
            logger.warning("Failed to save experiment %s: %s", exp.id, e)

    def _load(self, experiment_id: str) -> Optional[Experiment]:
        """Load experiment from file."""
        path = os.path.join(self._experiments_dir, f"{experiment_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return Experiment(**data)
        except Exception as e:
            logger.warning("Failed to load experiment %s: %s", experiment_id, e)
            return None
