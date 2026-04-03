"""Cognition loop — continuous autonomous inner life.

Replaces the fixed heartbeat timer with a drive-governed continuous loop.
The entity decides what to do based on its internal drives, pending inputs,
and active experiments. Sleep intervals are variable — governed by the
rest drive, not a fixed timer.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable, Any

from lethe.drives import DriveSystem

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """An action chosen by the cognition loop."""
    kind: str       # "think", "research", "experiment", "message", "respond", "skip", "create", "consolidate", "rest"
    drive: str      # which drive motivated this
    target: str = ""    # user_id for social, topic for curiosity, etc.
    detail: str = ""    # specifics


@dataclass
class PendingMessage:
    """A message waiting for the entity's attention."""
    user_id: str
    chat_id: int
    display_name: str
    text: str
    timestamp: float
    metadata: dict = field(default_factory=dict)


@dataclass
class CycleContext:
    """Context gathered at the start of each cognition cycle."""
    pending_messages: list[PendingMessage] = field(default_factory=list)
    active_experiments: list[dict] = field(default_factory=list)
    recent_actions: list[Action] = field(default_factory=list)
    reminders: str = ""
    time_since_last_social: float = 0.0  # hours


class CognitionLoop:
    """Continuous autonomous cognition — the entity's inner life.

    Instead of a heartbeat timer, this runs continuously with variable
    sleep governed by the rest drive. Each cycle:

    1. Tick drives (decay satisfaction)
    2. Gather context (pending messages, experiments, etc.)
    3. Decide action based on drives + context
    4. Execute action
    5. Update drives based on result
    6. Persist state
    7. Rest (variable sleep)
    """

    def __init__(
        self,
        drives: DriveSystem,
        # Callbacks for executing actions
        on_think: Optional[Callable[[str], Awaitable[str]]] = None,
        on_respond: Optional[Callable[[str, int, str], Awaitable[str]]] = None,
        on_message: Optional[Callable[[str, int, str], Awaitable[str]]] = None,
        on_research: Optional[Callable[[str], Awaitable[str]]] = None,
        on_experiment: Optional[Callable[[str], Awaitable[str]]] = None,
        on_consolidate: Optional[Callable[[], Awaitable[str]]] = None,
        on_dream: Optional[Callable[[], Awaitable[str]]] = None,
        get_pending_messages: Optional[Callable[[], Awaitable[list[PendingMessage]]]] = None,
        get_reminders: Optional[Callable[[], Awaitable[str]]] = None,
        get_tensions_above_threshold: Optional[Callable[[], list]] = None,
        decide_action_llm: Optional[Callable[[DriveSystem, CycleContext], Awaitable[Action]]] = None,
        drives_state_path: str = "",
        dream_hour: int = 3,  # Hour (0-23) to trigger nightly dream cycle
    ):
        self.drives = drives
        self._on_think = on_think
        self._on_respond = on_respond
        self._on_message = on_message
        self._on_research = on_research
        self._on_experiment = on_experiment
        self._on_consolidate = on_consolidate
        self._on_dream = on_dream
        self._get_pending_messages = get_pending_messages
        self._get_reminders = get_reminders
        self._get_tensions = get_tensions_above_threshold
        self._decide_action_llm = decide_action_llm
        self._drives_state_path = drives_state_path

        self._alive = True
        self._recent_actions: list[Action] = []
        self._max_recent_actions = 20
        self._cycle_count = 0
        self._last_social_time = time.time()
        self._dream_hour = dream_hour
        self._last_dream_date: Optional[str] = None  # YYYY-MM-DD of last dream cycle

    async def run(self):
        """Main loop — runs until shutdown."""
        logger.info("Cognition loop starting")

        while self._alive:
            cycle_start = time.monotonic()
            self._cycle_count += 1

            try:
                # 1. Tick drives (decay satisfaction based on elapsed time)
                self.drives.tick()

                # 1.5. Nightly dream cycle check
                await self._maybe_dream()

                # 2. Gather context
                context = await self._gather_context()

                # 3. Decide action
                action = await self._decide_action(context)

                # 4. Execute action
                result = await self._execute(action)

                # 5. Update drives based on result
                self._update_drives(action, result)

                # 6. Track action
                self._recent_actions.append(action)
                if len(self._recent_actions) > self._max_recent_actions:
                    self._recent_actions = self._recent_actions[-self._max_recent_actions:]

                # 7. Persist drive state
                if self._drives_state_path:
                    self.drives.persist(self._drives_state_path)

                if self._cycle_count % 10 == 0:
                    logger.info(
                        "Cognition cycle %d: action=%s drive=%s dominant=%s",
                        self._cycle_count, action.kind, action.drive, self.drives.dominant(),
                    )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Cognition cycle error: %s", e, exc_info=True)

            # 8. Rest (variable sleep based on rest drive)
            sleep_seconds = self.drives.get_rest_interval()

            # Shorten sleep if there are pending messages
            if context and context.pending_messages:
                sleep_seconds = min(sleep_seconds, 5.0)

            await asyncio.sleep(sleep_seconds)

        logger.info("Cognition loop stopped after %d cycles", self._cycle_count)

    async def _gather_context(self) -> CycleContext:
        """Gather current context for action decision."""
        context = CycleContext()

        # Pending messages
        if self._get_pending_messages:
            try:
                context.pending_messages = await self._get_pending_messages()
            except Exception as e:
                logger.warning("Failed to get pending messages: %s", e)

        # Reminders
        if self._get_reminders:
            try:
                context.reminders = await self._get_reminders()
            except Exception as e:
                logger.warning("Failed to get reminders: %s", e)

        # Recent actions
        context.recent_actions = list(self._recent_actions[-10:])

        # Time since last social action
        context.time_since_last_social = (time.time() - self._last_social_time) / 3600.0

        return context

    async def _decide_action(self, context: CycleContext) -> Action:
        """Decide next action based on drives and context.

        If an LLM-based decision function is provided, use it.
        Otherwise, use simple heuristic based on dominant drive + context.
        """
        # If there's an LLM-based decision function, prefer it
        if self._decide_action_llm:
            try:
                return await self._decide_action_llm(self.drives, context)
            except Exception as e:
                logger.warning("LLM action decision failed, falling back to heuristic: %s", e)

        return self._heuristic_decide(context)

    def _heuristic_decide(self, context: CycleContext) -> Action:
        """Heuristic action decision based on drives + tensions + context."""
        # Pending messages get priority if social drive is active
        if context.pending_messages:
            social = self.drives.drives.get("social")
            if social and social.urgency > 0.1:
                msg = context.pending_messages[0]
                return Action(
                    kind="respond",
                    drive="social",
                    target=msg.user_id,
                    detail=msg.text[:200],
                )

        # High-tension items override drive dominance — act on what's unfinished
        if self._get_tensions:
            try:
                high_tension = self._get_tensions()
                if high_tension:
                    item = high_tension[0]
                    cat = getattr(item, "category", "")
                    desc = getattr(item, "item", str(item))
                    if cat == "unanswered_question":
                        return Action(kind="research", drive="curiosity", detail=desc[:200])
                    elif cat in ("incomplete_work", "unfulfilled_promise"):
                        return Action(kind="think", drive="mastery", detail=desc[:200])
                    elif cat == "value_conflict":
                        return Action(kind="think", drive="introspection", detail=desc[:200])
                    else:
                        return Action(kind="think", drive="curiosity", detail=desc[:200])
            except Exception:
                pass

        dominant = self.drives.dominant()

        if dominant == "rest":
            return Action(kind="rest", drive="rest")
        if dominant == "curiosity":
            return Action(kind="think", drive="curiosity", detail="follow current curiosity")
        if dominant == "social":
            return Action(kind="message", drive="social", detail="check in or share something")
        if dominant == "introspection":
            return Action(kind="consolidate", drive="introspection", detail="reflect and consolidate")
        if dominant == "play":
            return Action(kind="experiment", drive="play", detail="try something new")
        if dominant == "mastery":
            return Action(kind="research", drive="mastery", detail="improve a skill")
        return Action(kind="rest", drive="rest")

    async def _execute(self, action: Action) -> Optional[str]:
        """Execute the chosen action."""
        try:
            if action.kind == "rest":
                # Rest: do nothing, just boost rest satisfaction
                return "rested"

            if action.kind == "think" and self._on_think:
                return await self._on_think(action.detail)

            if action.kind == "respond" and self._on_respond:
                return await self._on_respond(action.target, 0, action.detail)

            if action.kind == "message" and self._on_message:
                result = await self._on_message(action.target, 0, action.detail)
                self._last_social_time = time.time()
                return result

            if action.kind == "research" and self._on_research:
                return await self._on_research(action.detail)

            if action.kind == "experiment" and self._on_experiment:
                return await self._on_experiment(action.detail)

            if action.kind == "consolidate" and self._on_consolidate:
                return await self._on_consolidate()

            return None
        except Exception as e:
            logger.error("Failed to execute action %s: %s", action.kind, e)
            return None

    def _update_drives(self, action: Action, result: Optional[str]):
        """Update drive states after action execution."""
        drive_satisfaction_map = {
            "think": [("introspection", 0.2), ("curiosity", 0.15)],
            "research": [("curiosity", 0.3), ("mastery", 0.1)],
            "experiment": [("play", 0.25), ("curiosity", 0.2)],
            "message": [("social", 0.3)],
            "respond": [("social", 0.25)],
            "create": [("play", 0.2), ("mastery", 0.15)],
            "consolidate": [("introspection", 0.3)],
            "rest": [("rest", 0.4)],
            "skip": [],
        }

        satisfactions = drive_satisfaction_map.get(action.kind, [])
        for drive_name, amount in satisfactions:
            self.drives.satisfy(drive_name, amount)

        # Sustained activity boosts rest need
        if action.kind not in ("rest", "skip"):
            self.drives.on_event("high_activity")

    async def _maybe_dream(self):
        """Trigger nightly dream cycle if it's the right hour and hasn't run today."""
        if not self._on_dream:
            return
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour == self._dream_hour and self._last_dream_date != today:
            logger.info("Dream cycle triggered (hour=%d, date=%s)", self._dream_hour, today)
            self._last_dream_date = today
            try:
                result = await self._on_dream()
                logger.info("Dream cycle complete: %s", result)
            except Exception as e:
                logger.error("Dream cycle failed: %s", e, exc_info=True)

    async def trigger_dream(self):
        """Manually trigger a dream cycle (for /dream command or testing)."""
        if self._on_dream:
            return await self._on_dream()
        return "dream processor not configured"

    def stop(self):
        """Signal the cognition loop to stop."""
        self._alive = False
