"""Default Mode Network (DMN) — persistent background subagent.

The DMN is always-on, triggered by heartbeats. It replaces the old
heartbeat-to-cortex pipeline with a dedicated thinking agent that:

- Scans goals, todos, reminders
- Reorganizes memory, writes reflections
- Self-improves (updates questions.md, project notes)
- Notifies cortex when something needs user attention
- Works in rounds: reads previous round's state, thinks, acts, saves state

Uses the AUX model by default for cost efficiency.
Can use explicit DMN model override when configured.
Uses aggressive prompt caching — its system prompt is stable.
"""

import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

from lethe.actor import Actor, ActorConfig, ActorRegistry, ActorState, ActorMessage
from lethe.actor.tools import create_actor_tools
from lethe.memory.llm import AsyncLLMClient, LLMConfig
from lethe.prompts import load_prompt_template
from lethe.utils import strip_model_tags

logger = logging.getLogger(__name__)

# Workspace root — resolved from env or default
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", os.path.expanduser("~/lethe"))

# State file — persists between rounds
DMN_STATE_FILE = os.path.join(WORKSPACE_DIR, "dmn_state.md")
IDEAS_FILE = os.path.join(WORKSPACE_DIR, "ideas.md")
QUESTIONS_FILE = os.path.join(WORKSPACE_DIR, "questions.md")
DMN_RESET_MARKER = os.path.join(WORKSPACE_DIR, ".dmn_state_reset_v1")
FORCE_DEEP_EVERY_N_ROUNDS = 4
IDEAS_STALE_HOURS = 3

DMN_SYSTEM_PROMPT_TEMPLATE = load_prompt_template(
    "dmn_system",
    fallback=(
        "You are DMN.\n"
        "<principal>\n{principal_context}\n</principal>\n"
        "Workspace: {workspace}\nHome: {home}\n"
        "Run periodic background rounds and terminate with concise results."
    ),
)


def get_dmn_system_prompt(principal_context: str = "") -> str:
    """Build DMN system prompt with resolved workspace paths."""
    principal = principal_context.strip() or (
        "You work to advance your principal's goals across projects, well-being, "
        "relationships, and long-term outcomes. Use memory blocks for current context."
    )
    return DMN_SYSTEM_PROMPT_TEMPLATE.format(
        workspace=WORKSPACE_DIR,
        home=os.path.expanduser("~"),
        principal_context=principal,
    )

DMN_ROUND_MESSAGE = load_prompt_template(
    "dmn_round_message",
    fallback=(
        "[DMN Round - {timestamp}]\n\n{reminders}\n{previous_state}\n{mode_directive}\n\n"
        "Run round and terminate with summary."
    ),
)


class DefaultModeNetwork:
    """Persistent background subagent that replaces heartbeats.
    
    The DMN is a special actor that:
    - Is spawned once at startup and re-spawned each heartbeat round
    - Uses aux model by default (or explicit DMN model override)
    - Has memory tools, file tools, todo tools
    - Can send messages to the cortex for user notifications
    - Persists state between rounds via a file
    """

    def __init__(
        self,
        registry: ActorRegistry,
        llm_factory: Callable,
        available_tools: dict,
        cortex_id: str,
        send_to_user: Callable[[str], Awaitable[None]],
        get_reminders: Optional[Callable[[], Awaitable[str]]] = None,
        principal_context_provider: Optional[Callable[[], str]] = None,
        model_override: str = "",
    ):
        self.registry = registry
        self.llm_factory = llm_factory
        self.available_tools = available_tools
        self.cortex_id = cortex_id
        self.send_to_user = send_to_user
        self.get_reminders = get_reminders
        self.principal_context_provider = principal_context_provider
        self.model_override = (model_override or "").strip()
        self._current_actor: Optional[Actor] = None
        self._status: dict = {
            "state": "idle",
            "rounds_total": 0,
            "last_started_at": "",
            "last_completed_at": "",
            "last_mode": "",
            "last_turns": 0,
            "last_forced_deep": False,
            "last_force_reason": "",
            "last_result": "",
            "last_user_notify": "",
            "last_error": "",
        }
        self._round_history: deque[dict] = deque(maxlen=40)

    @staticmethod
    def _extract_user_notification(messages: list[ActorMessage], cortex_id: str) -> Optional[str]:
        """Extract latest explicit user notification from structured metadata."""
        candidates = []
        for m in messages:
            if m.recipient != cortex_id or m.sender == cortex_id:
                continue
            if m.metadata.get("channel") != "user_notify":
                continue
            text = (m.content or "").strip()
            if text:
                candidates.append(text)
        return candidates[-1] if candidates else None

    async def run_round(self) -> Optional[str]:
        """Execute one DMN round. Called by heartbeat timer.
        
        Returns:
            Message to send to user, or None if nothing urgent
        """
        round_started_at = datetime.now(timezone.utc)
        timestamp = round_started_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        self._status["state"] = "running"
        self._status["last_started_at"] = round_started_at.isoformat()
        self._status["last_error"] = ""
        
        # Get reminders
        reminders_text = ""
        if self.get_reminders:
            try:
                reminders = await self.get_reminders()
                if reminders:
                    reminders_text = f"Active reminders:\n{reminders}\n"
            except Exception as e:
                logger.warning(f"DMN: failed to get reminders: {e}")
        
        # Read previous state
        previous_state = ""
        try:
            if os.path.exists(DMN_STATE_FILE):
                with open(DMN_STATE_FILE, "r") as f:
                    content = f.read().strip()
                    if content:
                        previous_state = f"Previous round state:\n{content}\n"
        except Exception as e:
            logger.warning(f"DMN: failed to read state: {e}")

        # One-time reset if DMN state appears stale/self-referential.
        reset_reason = self._stale_state_reason(previous_state)
        if reset_reason:
            reset_body = (
                "# DMN State — Reset Baseline\n"
                f"*Reset at: {timestamp}*\n\n"
                "## Why reset happened\n"
                f"- {reset_reason}\n\n"
                "## Next focus\n"
                "- Re-anchor to current memory blocks and active project files\n"
                "- Produce at least one concrete idea in ideas.md\n"
                "- Update questions.md only if there is a novel pattern\n"
            )
            try:
                with open(DMN_STATE_FILE, "w") as f:
                    f.write(reset_body)
                with open(DMN_RESET_MARKER, "w") as f:
                    f.write(timestamp)
                previous_state = f"Previous round state:\n{reset_body}\n"
                logger.warning(f"DMN: state reset triggered ({reset_reason})")
            except Exception as e:
                logger.warning(f"DMN: failed to write reset state: {e}")

        force_deep, force_reason = self._should_force_deep(previous_state)
        mode_directive = (
            f"[MODE DIRECTIVE] FORCE DEEP MODE THIS ROUND. Reason: {force_reason}. "
            f"Write at least one idea entry to {IDEAS_FILE} and summarize why it matters."
            if force_deep else
            "[MODE DIRECTIVE] Choose QUICK vs DEEP normally based on detected opportunities."
        )

        file_stats_before = self._snapshot_files()
        
        # Create the DMN actor for this round
        config = ActorConfig(
            name="dmn",
            group="main",
            goals="Background thinking round. Scan goals, reflect, take action, update state.",
            model="",  # Will be set to main model by factory
            tools=["read_file", "write_file", "edit_file", "list_directory",
                   "grep_search", "bash", "web_search", "fetch_webpage",
                   "memory_read", "memory_update", "memory_append",
                   "archival_search", "archival_insert", "conversation_search"],
            max_turns=12,  # Increased for deep thinking mode; most rounds should still be quick (2-3 turns)
        )
        
        actor = self.registry.spawn(config, spawned_by=self.cortex_id)
        self._current_actor = actor
        
        # Create LLM client with MAIN model and DMN system prompt
        llm = await self._create_dmn_llm(actor)
        actor._llm = llm
        
        # Register tools
        actor_tools = create_actor_tools(actor, self.registry)
        for func, _ in actor_tools:
            llm.add_tool(func)
        
        for tool_name in config.tools:
            if tool_name in self.available_tools:
                func, schema = self.available_tools[tool_name]
                llm.add_tool(func, schema)
        
        # Build round message
        message = DMN_ROUND_MESSAGE.format(
            timestamp=timestamp,
            reminders=reminders_text,
            previous_state=previous_state,
            mode_directive=mode_directive,
        )
        
        # Periodic cleanup: remove actors terminated > 1 hour ago
        self.registry.cleanup_terminated()
        
        logger.info(f"DMN round starting ({len(llm._tools)} tools)")
        
        # Run the round
        user_message = None
        try:
            for turn in range(config.max_turns):
                actor._turns = turn + 1
                if actor.state == ActorState.TERMINATED:
                    break
                
                # Check inbox for messages (from cortex)
                incoming = []
                while not actor._inbox.empty():
                    try:
                        msg = actor._inbox.get_nowait()
                        incoming.append(msg)
                    except asyncio.QueueEmpty:
                        break
                
                if turn == 0:
                    turn_message = message
                elif incoming:
                    parts = [f"[From {m.sender}]: {m.content}" for m in incoming]
                    turn_message = "\n".join(parts)
                else:
                    turn_message = "[Continue your round. Call terminate(result) when done.]"
                
                try:
                    response = await llm.chat(turn_message, max_tool_iterations=5)
                except Exception as e:
                    logger.error(f"DMN LLM error: {e}")
                    break

                # Check if DMN sent an explicit user notification to cortex
                event_notifies = self.registry.events.query(
                    event_type="user_notify",
                    actor_id=actor.id,
                    since=round_started_at,
                    limit=5,
                )
                if event_notifies:
                    user_message = event_notifies[-1].payload.get("message", "") or user_message
                else:
                    user_message = self._extract_user_notification(actor._messages, self.cortex_id) or user_message

                if actor.state == ActorState.TERMINATED:
                    break
            
            # Force terminate if didn't self-terminate
            if actor.state != ActorState.TERMINATED:
                actor.terminate(f"Round complete (turn {turn + 1})")
            
        except Exception as e:
            logger.error(f"DMN round error: {e}", exc_info=True)
            self._status["last_error"] = str(e)
            if actor.state != ActorState.TERMINATED:
                actor.terminate(f"Error: {e}")
        
        result = actor._result or "No result"
        file_stats_after = self._snapshot_files()
        mode = "DEEP" if "DEEP" in result.upper() or actor._turns > 4 else "QUICK"
        touched = self._diff_file_stats(file_stats_before, file_stats_after)
        round_completed_at = datetime.now(timezone.utc)
        duration_seconds = (round_completed_at - round_started_at).total_seconds()
        logger.info(
            "DMN telemetry: mode=%s turns=%s forced=%s reason=%s touched=%s",
            mode,
            actor._turns,
            force_deep,
            force_reason or "none",
            touched or "none",
        )
        logger.info(f"DMN round complete: {result[:100]}")
        self._status["rounds_total"] += 1
        self._status["last_completed_at"] = round_completed_at.isoformat()
        self._status["last_mode"] = mode
        self._status["last_turns"] = actor._turns
        self._status["last_forced_deep"] = force_deep
        self._status["last_force_reason"] = force_reason
        self._status["last_result"] = result[:240]
        self._round_history.append(
            {
                "started_at": round_started_at.isoformat(),
                "completed_at": round_completed_at.isoformat(),
                "mode": mode,
                "turns": int(actor._turns),
                "duration_seconds": round(duration_seconds, 2),
                "forced_deep": bool(force_deep),
                "force_reason": force_reason or "",
                "touched": touched or "",
                "user_notify": bool(user_message),
                "error": self._status.get("last_error", ""),
                "result": result[:240],
            }
        )
        
        # Clean up
        self._current_actor = None
        
        # DMN never sends directly to user; cortex decides if/when to forward.
        if user_message:
            self._status["last_user_notify"] = user_message[:240]
        self._status["state"] = "idle"
        return None

    async def _create_dmn_llm(self, actor: Actor) -> AsyncLLMClient:
        """Create LLM client for DMN with configured/default model and stable prompt."""
        config = LLMConfig()
        if self.model_override:
            config.model = self.model_override
        elif config.model_aux:
            config.model = config.model_aux
        
        # Reasonable context for background work
        config.context_limit = min(config.context_limit, 64000)
        config.max_output_tokens = min(config.max_output_tokens, 4096)
        
        principal_context = ""
        if self.principal_context_provider:
            try:
                principal_context = self.principal_context_provider() or ""
            except Exception as e:
                logger.warning(f"DMN: failed to get principal context: {e}")

        client = AsyncLLMClient(
            config=config,
            system_prompt=get_dmn_system_prompt(principal_context=principal_context),
            usage_scope="dmn",
        )
        
        return client

    def _extract_round_number(self, previous_state: str) -> int:
        if not previous_state:
            return 0
        match = re.search(r"Round\s+(\d+)", previous_state)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _should_force_deep(self, previous_state: str) -> tuple[bool, str]:
        last_round = self._extract_round_number(previous_state)
        if last_round and last_round % FORCE_DEEP_EVERY_N_ROUNDS == 0:
            return True, f"periodic deep cadence (every {FORCE_DEEP_EVERY_N_ROUNDS} rounds)"

        try:
            if os.path.exists(IDEAS_FILE):
                age_seconds = datetime.now(timezone.utc).timestamp() - os.path.getmtime(IDEAS_FILE)
                if age_seconds > IDEAS_STALE_HOURS * 3600:
                    hours = int(age_seconds // 3600)
                    return True, f"ideas.md stale for {hours}h"
        except Exception:
            pass
        return False, ""

    def _stale_state_reason(self, previous_state: str) -> str:
        if not previous_state:
            return ""
        if os.path.exists(DMN_RESET_MARKER):
            return ""
        stale_markers = ("Valentine", "no changes", "all stable")
        marker_hits = sum(1 for marker in stale_markers if marker.lower() in previous_state.lower())
        quick_hits = previous_state.lower().count("quick check")
        if marker_hits >= 2 or quick_hits >= 4:
            return "state appears repetitive/stale and anchored to old context"
        return ""

    def _snapshot_files(self) -> dict:
        snapshot = {}
        for path in (DMN_STATE_FILE, IDEAS_FILE, QUESTIONS_FILE):
            try:
                if os.path.exists(path):
                    snapshot[path] = {
                        "mtime": os.path.getmtime(path),
                        "size": os.path.getsize(path),
                    }
                else:
                    snapshot[path] = {"mtime": 0, "size": 0}
            except Exception:
                snapshot[path] = {"mtime": 0, "size": 0}
        return snapshot

    def _diff_file_stats(self, before: dict, after: dict) -> str:
        changed = []
        for path, info_after in after.items():
            info_before = before.get(path, {"mtime": 0, "size": 0})
            if info_after["mtime"] != info_before["mtime"] or info_after["size"] != info_before["size"]:
                base = os.path.basename(path)
                changed.append(f"{base}({info_before['size']}->{info_after['size']} bytes)")
        return ", ".join(changed)

    @property
    def status(self) -> dict:
        """Current DMN runtime status for monitoring surfaces."""
        status = dict(self._status)
        status["round_history"] = list(self._round_history)
        return status

    @staticmethod
    def _take_lines(text: str, max_lines: int) -> str:
        if not text:
            return ""
        lines = str(text).splitlines()
        if len(lines) <= max_lines:
            return str(text)
        return "\n".join(lines[:max_lines]) + "\n...[truncated by lines]"

    def get_context_view(self, max_lines: int = 220) -> str:
        """Build a dashboard-friendly DMN context snapshot."""
        try:
            if os.path.exists(DMN_STATE_FILE):
                with open(DMN_STATE_FILE, "r") as f:
                    state_text = f.read()
            else:
                state_text = "(dmn_state.md not found)"
        except Exception as e:
            state_text = f"(failed to read dmn_state.md: {e})"

        principal_context = ""
        if self.principal_context_provider:
            try:
                principal_context = self.principal_context_provider() or ""
            except Exception as e:
                principal_context = f"(failed to get principal context: {e})"

        status = self.status
        lines = [
            "# DMN Context",
            "",
            f"- state: {status.get('state', 'idle')}",
            f"- rounds_total: {status.get('rounds_total', 0)}",
            f"- last_mode: {status.get('last_mode', '-')}",
            f"- last_turns: {status.get('last_turns', 0)}",
            f"- last_started_at: {status.get('last_started_at') or '-'}",
            f"- last_completed_at: {status.get('last_completed_at') or '-'}",
            f"- last_error: {status.get('last_error') or '-'}",
            "",
            "## Principal context snapshot",
            self._take_lines(principal_context, max_lines=80) or "(none)",
            "",
            "## dmn_state.md",
            self._take_lines(state_text, max_lines=max_lines) if state_text else "(empty)",
        ]
        return "\n".join(lines)
