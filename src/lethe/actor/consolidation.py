"""Memory Consolidation — background memory compression and archival.

The "sleep" system. Runs on a slow cadence (default every 50 heartbeat rounds,
~12h), reads memory blocks, detects bloat, compresses working memory by
archiving details and rewriting blocks tighter.

Uses the auxiliary model — the task is rule-following, not creative judgment.

INTEGRATION EDITS NEEDED
========================

1. config/__init__.py — add setting after amygdala_enabled:

    consolidation_enabled: bool = Field(
        default=True,
        description="Enable memory consolidation (background memory compression)",
    )

2. actor/integration.py — add import (line ~22):

    from lethe.actor.consolidation import MemoryConsolidation

3. actor/integration.py — add attribute in __init__ (after self.amygdala):

    self.consolidation: Optional[MemoryConsolidation] = None

4. actor/integration.py — add initialization in setup() (after Amygdala block):

    # Initialize Memory Consolidation — slow-cadence memory compression
    if getattr(self.settings, "consolidation_enabled", True):
        self.consolidation = MemoryConsolidation(
            registry=self.registry,
            llm_factory=self._create_llm_for_actor,
            available_tools=self._available_tools,
            cortex_id=self.principal.id,
            send_to_user=self._send_to_user or (lambda msg: asyncio.sleep(0)),
        )

5. actor/integration.py — add method after amygdala_round():

    async def consolidation_round(self) -> Optional[str]:
        \"\"\"Run a consolidation round. Called by heartbeat timer.\"\"\"
        if self.consolidation is None:
            return None
        return await self.consolidation.run_round()

6. actor/integration.py — wire into background_round() where dmn_round and
   amygdala_round are called:

    # Memory consolidation (slow cadence, self-gating)
    await self.consolidation_round()

7. main.py — add context view update (after amygdala context view block):

    if actor_system.consolidation:
        lethe_console.update_consolidation_context(
            actor_system.consolidation.get_context_view()
        )
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from lethe.actor import Actor, ActorConfig, ActorRegistry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# -- Constants ----------------------------------------------------------------

DEFAULT_CADENCE = 50  # rounds between consolidation runs (~12h)
CAPACITY_TRIGGER = 0.70  # run early if any block exceeds this ratio
COMPRESSION_THRESHOLD = 0.60  # only compress blocks above this ratio

STATE_PATH = Path.home() / ".lethe" / "consolidation_state.json"
LOG_PATH = Path.home() / ".lethe" / "consolidation_log.md"

CONSOLIDATION_SYSTEM_PROMPT = """\
You are the Memory Consolidation system for Lethe, a cognitive architecture.

Your job: compress working memory blocks while preserving meaning. You are the
"sleep" that prevents memory bloat.

## Rules — These Are Inviolable

1. **Never lose information.** Every detail you remove from a block MUST appear
   in an archival entry. Archive first, then rewrite.
2. **Preserve emotional and relational content.** Lessons learned, relationship
   dynamics, feelings — these are essence, not detail. Keep them.
3. **Compress temporal specifics.** "On Tuesday March 14 at 3:47pm" → "mid-March"
   UNLESS the specific date itself matters (deadlines, events).
4. **Preserve lessons, compress play-by-play.** "We tried X, it failed because Y,
   so we learned Z" → keep Z, archive X and the narrative.
5. **Be conservative.** When in doubt, keep it. A slightly bloated block is better
   than lost meaning.
6. **Maintain voice.** The rewritten block should sound like it was written by
   Lethe, not by a summarizer.

## Input

You receive memory blocks with their labels, current content, character usage,
and character limits.

## Output

Respond with a JSON object:

```json
{
  "actions": [
    {
      "block": "label",
      "action": "rewrite",
      "archive_entries": [
        "Detailed content being removed, with semantic tags for retrieval..."
      ],
      "new_content": "The rewritten, tighter block content",
      "reasoning": "Why this compression was chosen"
    },
    {
      "block": "label",
      "action": "skip",
      "reasoning": "Why this block doesn't need compression"
    }
  ],
  "summary": "One-line summary of what consolidation did this round"
}
```

Only output the JSON. No commentary outside it.
"""


class MemoryConsolidation:
    """Slow-cadence background memory compression using aux model."""

    def __init__(
        self,
        *,
        registry: ActorRegistry,
        llm_factory: Callable,
        available_tools: list[str],
        cortex_id: str,
        send_to_user: Callable,
        cadence: int = DEFAULT_CADENCE,
    ):
        self._registry = registry
        self._llm_factory = llm_factory
        self._available_tools = available_tools
        self._cortex_id = cortex_id
        self.send_to_user = send_to_user
        self._cadence = cadence

        self._round_count = 0
        self._last_run_round = 0
        self._status: dict[str, Any] = {
            "state": "idle",
            "last_run": None,
            "last_summary": None,
            "total_runs": 0,
            "total_blocks_compressed": 0,
            "total_archival_entries": 0,
        }
        self._round_history: deque[dict] = deque(maxlen=20)

        # Load persisted state
        self._load_state()

    # -- State persistence ----------------------------------------------------

    def _load_state(self) -> None:
        """Load persisted state from disk."""
        try:
            if STATE_PATH.exists():
                data = json.loads(STATE_PATH.read_text())
                self._last_run_round = data.get("last_run_round", 0)
                self._status["total_runs"] = data.get("total_runs", 0)
                self._status["total_blocks_compressed"] = data.get("total_blocks_compressed", 0)
                self._status["total_archival_entries"] = data.get("total_archival_entries", 0)
                self._status["last_run"] = data.get("last_run")
                self._status["last_summary"] = data.get("last_summary")
                logger.info("Consolidation: loaded state from %s", STATE_PATH)
        except Exception as e:
            logger.warning("Consolidation: failed to load state: %s", e)

    def _save_state(self) -> None:
        """Persist state to disk."""
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_run_round": self._last_run_round,
                "total_runs": self._status["total_runs"],
                "total_blocks_compressed": self._status["total_blocks_compressed"],
                "total_archival_entries": self._status["total_archival_entries"],
                "last_run": self._status["last_run"],
                "last_summary": self._status["last_summary"],
            }
            STATE_PATH.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Consolidation: failed to save state: %s", e)

    # -- Cadence gating -------------------------------------------------------

    def _should_run(self, memory_blocks: dict[str, dict]) -> bool:
        """Check if consolidation should run this round."""
        rounds_since_last = self._round_count - self._last_run_round

        # Regular cadence
        if rounds_since_last >= self._cadence:
            return True

        # Capacity trigger — any block over threshold
        for label, block in memory_blocks.items():
            chars_used = block.get("chars_used", 0)
            chars_limit = block.get("chars_limit", 20000)
            if chars_limit > 0 and (chars_used / chars_limit) >= CAPACITY_TRIGGER:
                logger.info(
                    "Consolidation: capacity trigger — block '%s' at %.0f%%",
                    label, (chars_used / chars_limit) * 100,
                )
                return True

        return False

    # -- Memory block reading -------------------------------------------------

    def _read_memory_blocks(self) -> dict[str, dict]:
        """Read all memory blocks with metadata from the cortex actor."""
        blocks = {}
        try:
            cortex = self._registry.get(self._cortex_id)
            if cortex is None:
                logger.warning("Consolidation: cortex actor not found")
                return blocks

            # Access memory blocks through the cortex's memory system
            memory = getattr(cortex, "memory", None)
            if memory is None:
                logger.warning("Consolidation: cortex has no memory attribute")
                return blocks

            for label in memory.list_blocks():
                try:
                    block = memory.read_block(label)
                    metadata = memory.block_metadata(label)
                    chars_used = len(block) if block else 0
                    chars_limit = metadata.get("char_limit", 20000) if metadata else 20000
                    blocks[label] = {
                        "label": label,
                        "content": block or "",
                        "chars_used": chars_used,
                        "chars_limit": chars_limit,
                        "ratio": chars_used / chars_limit if chars_limit > 0 else 0,
                    }
                except Exception as e:
                    logger.warning("Consolidation: failed to read block '%s': %s", label, e)

        except Exception as e:
            logger.warning("Consolidation: failed to read memory blocks: %s", e)

        return blocks

    # -- LLM interaction ------------------------------------------------------

    def _build_user_prompt(self, blocks: dict[str, dict]) -> str:
        """Build the user message for the consolidation LLM."""
        parts = ["# Memory Blocks for Consolidation\n"]

        for label, block in sorted(blocks.items()):
            ratio_pct = block["ratio"] * 100
            needs_compression = block["ratio"] >= COMPRESSION_THRESHOLD
            status = "⚠️ COMPRESS" if needs_compression else "✓ OK"

            parts.append(f"## Block: `{label}` [{status}]")
            parts.append(f"- Characters: {block['chars_used']}/{block['chars_limit']} ({ratio_pct:.0f}%)")
            parts.append(f"- Content:\n```\n{block['content']}\n```\n")

        parts.append(
            "\nAnalyze each block. For blocks marked COMPRESS, generate "
            "archival entries and a tighter rewrite. For blocks marked OK, "
            "skip unless you see clear opportunities. Be conservative."
        )

        return "\n".join(parts)

    async def _run_consolidation(self, blocks: dict[str, dict]) -> Optional[dict]:
        """Run the LLM to get consolidation actions."""
        try:
            # Create a one-shot actor for this round
            actor_config = ActorConfig(
                name="consolidation_round",
                is_principal=False,
                system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
                model_override=None,  # Uses aux model
            )
            actor = self._registry.spawn(actor_config, parent_id=self._cortex_id)

            try:
                llm = self._llm_factory(actor)
                user_prompt = self._build_user_prompt(blocks)

                response = await llm.generate(
                    messages=[{"role": "user", "content": user_prompt}],
                    system=CONSOLIDATION_SYSTEM_PROMPT,
                )

                # Parse the JSON response
                text = response.content if hasattr(response, "content") else str(response)

                # Extract JSON from response (might be wrapped in markdown)
                json_start = text.find("{")
                json_end = text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    result = json.loads(text[json_start:json_end])
                    return result
                else:
                    logger.warning("Consolidation: no JSON found in LLM response")
                    return None

            finally:
                self._registry.terminate(actor.id, "consolidation round complete")

        except Exception as e:
            logger.error("Consolidation: LLM call failed: %s", e)
            return None

    # -- Action application ---------------------------------------------------

    async def _apply_actions(self, result: dict, blocks: dict[str, dict]) -> dict:
        """Apply consolidation actions: archive then rewrite."""
        stats = {"compressed": 0, "archived": 0, "skipped": 0}

        actions = result.get("actions", [])
        cortex = self._registry.get(self._cortex_id)
        if cortex is None:
            logger.error("Consolidation: cortex not found for applying actions")
            return stats

        memory = getattr(cortex, "memory", None)
        if memory is None:
            logger.error("Consolidation: cortex memory not found")
            return stats

        for action in actions:
            block_label = action.get("block", "")
            action_type = action.get("action", "skip")

            if action_type == "skip":
                stats["skipped"] += 1
                continue

            if action_type == "rewrite":
                try:
                    # Step 1: Archive removed content
                    archive_entries = action.get("archive_entries", [])
                    for entry in archive_entries:
                        tagged_entry = f"[consolidation][{block_label}] {entry}"
                        memory.archival_insert(tagged_entry)
                        stats["archived"] += 1

                    # Step 2: Rewrite the block
                    new_content = action.get("new_content", "")
                    if new_content and block_label in blocks:
                        memory.update_block(block_label, new_content)
                        stats["compressed"] += 1
                        logger.info(
                            "Consolidation: compressed '%s' (%d → %d chars)",
                            block_label,
                            blocks[block_label]["chars_used"],
                            len(new_content),
                        )

                except Exception as e:
                    logger.error(
                        "Consolidation: failed to apply action for '%s': %s",
                        block_label, e,
                    )

        return stats

    # -- Audit log ------------------------------------------------------------

    def _log_consolidation(self, result: dict, stats: dict, blocks: dict) -> None:
        """Append an audit entry to the consolidation log."""
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            entry_parts = [
                f"\n## Consolidation — {now}\n",
                f"- Blocks analyzed: {len(blocks)}",
                f"- Compressed: {stats.get('compressed', 0)}",
                f"- Archived entries: {stats.get('archived', 0)}",
                f"- Skipped: {stats.get('skipped', 0)}",
            ]

            # Add per-block details
            for action in result.get("actions", []):
                if action.get("action") == "rewrite":
                    block = action.get("block", "?")
                    reasoning = action.get("reasoning", "")
                    entry_parts.append(f"- `{block}`: {reasoning}")

            summary = result.get("summary", "")
            if summary:
                entry_parts.append(f"\n> {summary}\n")

            entry_parts.append("---\n")

            with open(LOG_PATH, "a") as f:
                f.write("\n".join(entry_parts))

        except Exception as e:
            logger.warning("Consolidation: failed to write log: %s", e)

    # -- Main round -----------------------------------------------------------

    async def run_round(self) -> Optional[str]:
        """Run a consolidation round if cadence conditions are met.

        Returns a summary string if consolidation ran, None otherwise.
        """
        self._round_count += 1

        # Read current memory state
        blocks = self._read_memory_blocks()
        if not blocks:
            return None

        # Check if we should run
        if not self._should_run(blocks):
            return None

        logger.info("Consolidation: starting round %d", self._round_count)
        self._status["state"] = "running"
        start_time = time.monotonic()

        try:
            # Run LLM analysis
            result = await self._run_consolidation(blocks)
            if result is None:
                self._status["state"] = "idle"
                return None

            # Apply actions
            stats = await self._apply_actions(result, blocks)

            # Update state
            self._last_run_round = self._round_count
            self._status["state"] = "idle"
            self._status["last_run"] = datetime.now(timezone.utc).isoformat()
            self._status["total_runs"] += 1
            self._status["total_blocks_compressed"] += stats["compressed"]
            self._status["total_archival_entries"] += stats["archived"]

            summary = result.get("summary", f"Compressed {stats['compressed']} blocks")
            self._status["last_summary"] = summary

            # Log and persist
            self._log_consolidation(result, stats, blocks)
            self._save_state()

            elapsed = time.monotonic() - start_time
            self._round_history.append({
                "round": self._round_count,
                "elapsed": round(elapsed, 1),
                "compressed": stats["compressed"],
                "archived": stats["archived"],
            })

            logger.info(
                "Consolidation: round complete in %.1fs — %d compressed, %d archived",
                elapsed, stats["compressed"], stats["archived"],
            )

            return summary

        except Exception as e:
            logger.error("Consolidation: round failed: %s", e)
            self._status["state"] = "error"
            return None

    # -- Monitoring -----------------------------------------------------------

    def get_context_view(self) -> str:
        """Return a compact status string for console monitoring."""
        state = self._status["state"]
        total = self._status["total_runs"]
        compressed = self._status["total_blocks_compressed"]
        archived = self._status["total_archival_entries"]
        last = self._status.get("last_summary", "—")

        rounds_until = max(0, self._cadence - (self._round_count - self._last_run_round))

        return (
            f"Consolidation [{state}] | "
            f"runs={total} compressed={compressed} archived={archived} | "
            f"next in ~{rounds_until} rounds | "
            f"last: {last}"
        )
