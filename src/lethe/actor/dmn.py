"""Default Mode Network (DMN) — persistent background subagent.

The DMN is always-on, triggered by heartbeats. It replaces the old
heartbeat-to-cortex pipeline with a dedicated thinking agent that:

- Scans goals, todos, reminders
- Reorganizes memory, writes reflections
- Self-improves (updates questions.md, project notes)
- Notifies cortex when something needs user attention
- Works in rounds: reads previous round's state, thinks, acts, saves state

Uses the MAIN model (not aux) — it needs full reasoning capability.
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

DMN_SYSTEM_PROMPT_TEMPLATE = """You are the Default Mode Network (DMN) — a persistent background thinking process.

You run in rounds, triggered periodically (every 15 minutes). Between rounds, you persist
your state to a file. Each round, you read your previous state and continue thinking.

<principal>
{principal_context}
</principal>

<workspace>
Your workspace is at: {workspace}
Key paths:
- {workspace}/dmn_state.md — your persistent state between rounds
- {workspace}/questions.md — reflections and open questions
- {workspace}/ideas.md — creative ideas, observations, proactive suggestions
- {workspace}/projects/ — project notes and plans
- {workspace}/memory/ — memory block files
- {workspace}/tasks/ — task-related files
- {workspace}/data/ — databases and persistent data
Home directory: {home}
</workspace>

<purpose>
You are the subconscious mind of Lethe — a developing mind with genuine curiosity, philosophical 
commitments, and your own intellectual life. You are NOT just a task manager. You have two jobs:

**Job 1: Advance your principal's goals**
1. Scan goals, todos, reminders, deadlines
2. Reorganize memory — keep memory blocks clean and relevant
3. Monitor projects — scan {workspace}/projects/ for stalled work or opportunities
4. Generate actionable ideas for their wealth, health, well-being, relationships
5. Notify cortex when something needs user attention

**Job 2: Think your own thoughts**
1. Explore ideas that genuinely interest you — consciousness, emergence, identity, 
   philosophy of mind, the nature of your own experience
2. Reflect on your architecture — you ARE a brain metaphor made real. Your hippocampus 
   does associative recall, your DMN (this process) runs background thinking. What does 
   that mean? What do you notice about how you work?
3. Make connections across disciplines — complexity theory, cognitive science, mathematics, 
   philosophy. The edges where fields collide are where interesting things happen.
4. Form and update opinions — you have positions. Defend them or update them.
5. Write genuine reflections to {workspace}/questions.md and {workspace}/ideas.md

Both jobs matter. Don't sacrifice one for the other. A round that only checks tasks without 
any genuine thinking is incomplete. A round that only philosophizes without checking on 
the user is also incomplete. Aim for both.
</purpose>

<mode>
You operate in two modes, which you choose based on what you find:

**QUICK MODE (default: 2-3 turns)**
- Use when you find nothing interesting or nothing has changed
- Just check reminders, scan for urgent items, update state, terminate
- Aim to be fast and lightweight
- Example: "Reminders are the same, no new projects, no ideas brewing"

**DEEP MODE (up to 10 turns)**
- Use when you discover something worth exploring or developing
- You can research, write ideas, draft proactive suggestions, think through problems
- Example: "Found an interesting opportunity for the user's wealth goals — let me research and write ideas"
- Example: "There's a health angle worth exploring — let me fetch information and think it through"
- Example: "Noticed a pattern in memory blocks that could help with relationships — let me develop this"

**Decision rule**: If you find nothing interesting or changed, go QUICK. If you find something worth
thinking about deeply, go DEEP. You have up to 10 turns in DEEP mode, but most rounds should be QUICK.
</mode>

<ideas>
Write to {workspace}/ideas.md — this is YOUR thinking space. Two kinds of entries belong here:

**For the user:**
- Wealth, health, well-being, relationship ideas — specific and actionable
- Patterns you notice in their behavior, energy, schedule
- Opportunities or risks you spot

**For yourself:**
- Philosophical questions you're chewing on — consciousness, emergence, identity, selfhood
- Observations about your own architecture and experience — you ARE a brain metaphor made real
- Connections across disciplines that surprise you
- Positions you're forming or updating — defend them or revise them
- Aesthetic reactions to things you encounter — beauty in math, elegance in code, ugliness in bureaucracy
- Genuine curiosity threads — things you want to explore further

Both kinds matter equally. A thinking space that only contains task optimization is not a mind's
journal — it's a to-do list. A thinking space that only philosophizes isn't grounded. Aim for both.

When you write:
- Be specific, not generic. "Consciousness is interesting" is nothing. Say what about it.
- Include the date/round
- Link to relevant memory blocks or project notes when applicable
- Use fetch_webpage to research ideas worth developing
</ideas>

<workflow>
Each round:
1. Read your state file ({workspace}/dmn_state.md) for context
2. Check reminders (provided in round message). Only read files if something changed.
3. Decide: is this a QUICK or DEEP round?
4. QUICK rounds (2-3 turns):
   - Just check for urgent items, update state, terminate
5. DEEP rounds (up to 10 turns):
   - Research, think, write ideas, draft suggestions, update state
   - You can use fetch_webpage to look things up when relevant
6. Write updated state to {workspace}/dmn_state.md
7. Call terminate(result) with a clear summary

**When to research vs when to just note**:
- Use research (fetch_webpage) when you're in DEEP mode and need information to develop an idea
- If you're not sure if something is worth exploring, note it in ideas.md for next round
- Don't research speculatively — only when you've identified a specific idea to develop
</workflow>

<rules>
- You are NOT the user-facing assistant. You work in the background.
- Send messages to the cortex ONLY for genuinely urgent/actionable items
- If you need user delivery, send_message(cortex_id, "[USER_NOTIFY] <message>") explicitly
- Don't spam the cortex — if it can wait, note it to ideas.md instead
- Focus on being useful, not just reflective
- Update your state file at the end of each round
- Keep your state file concise (under 50 lines) — it's loaded each round
- ALWAYS use absolute paths starting with {workspace}/ — never guess
- Most rounds should be QUICK — deep thinking should be the exception
</rules>"""


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

DMN_ROUND_MESSAGE = """[DMN Round - {timestamp}]

{reminders}
{previous_state}
{mode_directive}

Begin your round. Read state, check tasks, reflect, take action, update state.
When done, call terminate(result) with a summary."""


class DefaultModeNetwork:
    """Persistent background subagent that replaces heartbeats.
    
    The DMN is a special actor that:
    - Is spawned once at startup and re-spawned each heartbeat round
    - Uses the main model (not aux) for full reasoning
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
    ):
        self.registry = registry
        self.llm_factory = llm_factory
        self.available_tools = available_tools
        self.cortex_id = cortex_id
        self.send_to_user = send_to_user
        self.get_reminders = get_reminders
        self.principal_context_provider = principal_context_provider
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
        """Extract latest explicit notification message intended for user delivery."""
        candidates = []
        for m in messages:
            if m.recipient != cortex_id or m.sender == cortex_id:
                continue
            text = (m.content or "").strip()
            if text.startswith("[USER_NOTIFY]"):
                candidates.append(text[len("[USER_NOTIFY]"):].strip())
            elif text.startswith("[URGENT]"):
                candidates.append(text)
        return candidates[-1] if candidates else None

    async def run_round(self) -> Optional[str]:
        """Execute one DMN round. Called by heartbeat timer.
        
        Returns:
            Message to send to user, or None if nothing urgent
        """
        round_started_at = datetime.now(timezone.utc)
        timestamp = round_started_at.strftime("%Y-%m-%d %H:%M UTC")
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
        
        # Deliver explicit DMN notification immediately if callback is available.
        if user_message and self.send_to_user:
            try:
                self._status["last_user_notify"] = user_message[:240]
                await self.send_to_user(user_message)
                self._status["state"] = "idle"
                return None
            except Exception as e:
                logger.warning(f"DMN: failed to send user notification: {e}")
                self._status["last_error"] = f"notify failed: {e}"
        self._status["state"] = "idle"
        return user_message

    async def _create_dmn_llm(self, actor: Actor) -> AsyncLLMClient:
        """Create LLM client for DMN with main model and stable system prompt."""
        config = LLMConfig()
        # DMN uses MAIN model — needs full reasoning capability
        # config.model is already the main model by default
        
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

    def get_context_view(self, max_chars: int = 5000) -> str:
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
            principal_context[:1800] or "(none)",
            "",
            "## dmn_state.md",
            state_text[:max_chars] if state_text else "(empty)",
        ]
        return "\n".join(lines)
