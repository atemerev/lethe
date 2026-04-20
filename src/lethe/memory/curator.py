"""Memory Curator — unified episodic memory management.

Replaces both the memory organizer and consolidation system. Two passes:

Pass 1 (Harvest): Walk recent conversation history, extract episodic memories
into archival storage. Tracks last-processed timestamp to avoid re-harvesting.

Pass 2 (Curate): Walk all episodic memories. Reorganize them (deduplicate,
merge related episodes, compress only if space pressure) using the main model.
Extract reusable knowledge into notes using the aux model. Content that becomes
a note is removed from the episodic memory to avoid duplication.

Runs on startup + every 6 hours (wall-clock timer, not round-counting).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from litellm import completion

from lethe.memory.notes import NoteStore, normalize_tags
from lethe.prompts import load_prompt_template

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".lethe" / "curator_state.json"
LOG_PATH = Path.home() / ".lethe" / "curator_log.md"

HARVEST_BATCH_SIZE = 100  # large batches — LLM needs full conversation context to synthesize
CURATE_BATCH_SIZE = 12
CADENCE_SECONDS = 6 * 3600  # 6 hours

HARVEST_PROMPT = load_prompt_template(
    "curator_harvest",
    fallback=(
        "You are extracting memories from a conversation transcript.\n"
        "Identify experiences, procedures, facts, lessons, relationship signals, and emotional context.\n"
        "Skip routine tool calls and mechanical exchanges — focus on what matters.\n"
        "Respond with a JSON array of objects:\n"
        '[{"text": "memory content", "tags": ["tag1", "tag2"]}]\n'
        "Each memory should be a self-contained episode that would be meaningful "
        "without the surrounding conversation. Empty array [] if nothing worth remembering."
    ),
)

CURATE_PROMPT = load_prompt_template(
    "curator_curate",
    fallback=(
        "You are curating a collection of episodic memories. Your job:\n\n"
        "1. **Identify duplicates** — memories that describe the same event/fact.\n"
        "   Merge them into one, keeping the richer version.\n"
        "2. **Identify stale memories** — events that have been superseded or are\n"
        "   no longer relevant. Mark for deletion.\n"
        "3. **Identify memories that should be compressed** — only if there are\n"
        "   many memories and space is a concern. Preserve meaning.\n"
        "4. **Retag** — fix inconsistent or missing tags.\n"
        "5. **Do NOT extract notes** — that is a separate step.\n\n"
        "Be conservative. When in doubt, keep the memory as-is.\n\n"
        "Respond with a JSON object:\n"
        "```json\n"
        '{\n'
        '  "actions": [\n'
        '    {"id": "mem-xxx", "action": "keep"},\n'
        '    {"id": "mem-xxx", "action": "update", "text": "new text", "tags": ["new", "tags"]},\n'
        '    {"id": "mem-xxx", "action": "merge_into", "target": "mem-yyy"},\n'
        '    {"id": "mem-xxx", "action": "delete", "reason": "superseded by ..."}\n'
        '  ],\n'
        '  "summary": "one-line summary of what changed"\n'
        '}\n'
        "```\n"
    ),
)

EXTRACT_NOTES_PROMPT = load_prompt_template(
    "curator_extract_notes",
    fallback=(
        "You are reviewing episodic memories to find reusable knowledge worth\n"
        "extracting into permanent notes.\n\n"
        "Notes are for: facts, procedures, contacts, skills, conventions —\n"
        "knowledge that is useful independent of when it was learned.\n\n"
        "Most memories should NOT become notes. Only extract when the memory\n"
        "contains crystallized, reusable knowledge.\n\n"
        "Respond with a JSON array. Empty array [] if nothing to extract:\n"
        "```json\n"
        '[\n'
        '  {\n'
        '    "source_id": "mem-xxx",\n'
        '    "title": "Note title",\n'
        '    "content": "Note content (rewritten as reference material)",\n'
        '    "tags": ["skill", "api"],\n'
        '    "remove_from_source": true\n'
        '  }\n'
        ']\n'
        "```\n"
        "Set remove_from_source to true if the extracted content fully covers\n"
        "what the memory said (so the memory can be deleted). False if the memory\n"
        "has episodic value beyond the extracted fact.\n"
    ),
)


def _get_model(tier: str = "aux") -> str:
    if tier == "main":
        return os.environ.get("LLM_MODEL", "")
    return os.environ.get("LLM_MODEL_AUX") or os.environ.get("LLM_MODEL", "")


def _get_api_base() -> str:
    return os.environ.get("LLM_API_BASE", "")


def _llm_call(prompt: str, user_content: str, model: str, max_tokens: int = 4000) -> str:
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    api_base = _get_api_base()
    if api_base:
        kwargs["api_base"] = api_base

    # Anthropic subscription auth
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token and "claude" in model.lower():
        kwargs["api_key"] = "placeholder"
        kwargs["extra_headers"] = {"Authorization": f"Bearer {auth_token}"}

    response = completion(**kwargs)
    return response.choices[0].message.content or ""


def _parse_json(text: str):
    import re
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
    # Find first [ or { and last ] or }
    start_arr = text.find("[")
    start_obj = text.find("{")
    if start_arr < 0 and start_obj < 0:
        return None
    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        end = text.rfind("]")
        if end > start_arr:
            return json.loads(text[start_arr:end + 1])
    if start_obj >= 0:
        end = text.rfind("}")
        if end > start_obj:
            return json.loads(text[start_obj:end + 1])
    return None


class MemoryCurator:
    """Unified memory curation: harvest from conversation, curate episodic memories."""

    def __init__(
        self,
        note_store: NoteStore,
        archival_memory,
        message_history,
    ):
        self.notes = note_store
        self.archival = archival_memory
        self.messages = message_history
        self._last_harvest_ts: Optional[str] = None
        self._last_run_ts: Optional[str] = None
        self._stats = {
            "total_runs": 0,
            "total_harvested": 0,
            "total_curated": 0,
            "total_notes_extracted": 0,
            "total_deleted": 0,
            "total_merged": 0,
        }
        self._load_state()

    def _load_state(self):
        try:
            if STATE_PATH.exists():
                data = json.loads(STATE_PATH.read_text())
                self._last_harvest_ts = data.get("last_harvest_ts")
                self._last_run_ts = data.get("last_run_ts")
                for k in self._stats:
                    if k in data:
                        self._stats[k] = data[k]
                logger.info("Curator: loaded state, last run: %s", self._last_run_ts)
        except Exception as e:
            logger.warning("Curator: failed to load state: %s", e)

    def _save_state(self):
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_harvest_ts": self._last_harvest_ts,
                "last_run_ts": self._last_run_ts,
                **self._stats,
            }
            STATE_PATH.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Curator: failed to save state: %s", e)

    def should_run(self) -> bool:
        if self._last_run_ts is None:
            return True
        try:
            last = datetime.fromisoformat(self._last_run_ts)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            return elapsed >= CADENCE_SECONDS
        except Exception:
            return True

    def run(self) -> dict:
        """Run both curator passes. Returns stats."""
        logger.info("Curator: starting run")
        start = time.monotonic()

        harvest_stats = self._pass_harvest()
        curate_stats = self._pass_curate()

        self._last_run_ts = datetime.now(timezone.utc).isoformat()
        self._stats["total_runs"] += 1
        self._save_state()

        elapsed = time.monotonic() - start
        result = {**harvest_stats, **curate_stats, "elapsed_s": round(elapsed, 1)}
        self._log_run(result)
        logger.info("Curator: done in %.1fs — %s", elapsed, result)
        return result

    # -- Pass 1: Harvest -------------------------------------------------------

    def _pass_harvest(self) -> dict:
        """Extract episodic memories from recent conversation history."""
        model = _get_model("aux")
        if not model:
            logger.warning("Curator: no model configured, skipping harvest")
            return {"harvested": 0}

        # Get messages after last harvest timestamp
        all_msgs = self.messages.get_recent(200)
        if self._last_harvest_ts:
            all_msgs = [m for m in all_msgs if m["created_at"] > self._last_harvest_ts]

        # Filter to user and assistant messages with substantive content
        msgs = []
        for m in all_msgs:
            if m["role"] not in ("user", "assistant"):
                continue
            content = m["content"]
            if isinstance(content, str) and len(content) > 20:
                msgs.append(m)

        if not msgs:
            logger.info("Curator: no substantive messages to harvest")
            return {"harvested": 0}

        # Collect existing tags for normalization
        existing_tags = self._collect_all_tags()
        harvested = 0

        # Overlap batches by 20% so knowledge at boundaries isn't lost
        overlap = max(5, HARVEST_BATCH_SIZE // 5)
        step = HARVEST_BATCH_SIZE - overlap

        for batch_start in range(0, len(msgs), step):
            batch = msgs[batch_start:batch_start + HARVEST_BATCH_SIZE]
            if not batch:
                break
            transcript = self._format_transcript(batch)

            try:
                raw = _llm_call(HARVEST_PROMPT, transcript, model)
                memories = _parse_json(raw)
                if not isinstance(memories, list):
                    continue

                for mem in memories:
                    text = mem.get("text", "").strip()
                    if not text or len(text) < 10:
                        continue
                    tags = mem.get("tags", [])
                    if not isinstance(tags, list):
                        tags = []
                    tags = [t.lower().strip() for t in tags if isinstance(t, str)]
                    tags = normalize_tags(tags, existing_tags)

                    self.archival.add(
                        text=text,
                        metadata={"source": "curator_harvest"},
                        tags=tags,
                    )
                    existing_tags.update(tags)
                    harvested += 1

            except Exception as e:
                logger.error("Curator: harvest batch failed: %s", e)

        # Update timestamp to latest processed message
        if msgs:
            self._last_harvest_ts = msgs[-1]["created_at"]
        self._stats["total_harvested"] += harvested
        self._save_state()

        logger.info("Curator: harvested %d episodic memories from %d messages", harvested, len(msgs))
        return {"harvested": harvested}

    def _collect_all_tags(self) -> set[str]:
        """Collect all tags from notes + archival for normalization."""
        tags = set()
        for note in self.notes.list_notes():
            for t in note.get("tags", []):
                tags.add(t.lower().strip())
        try:
            table = self.archival._get_table()
            for row in table.to_pandas().to_dict("records"):
                raw = row.get("tags", "[]")
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        parsed = []
                else:
                    parsed = raw or []
                for t in parsed:
                    tags.add(str(t).lower().strip())
        except Exception:
            pass
        return tags

    def _format_transcript(self, messages: list[dict]) -> str:
        parts = []
        for m in messages:
            ts = m["created_at"][:16].replace("T", " ")
            role = m["role"].upper()
            content = m["content"]
            if isinstance(content, str) and len(content) > 600:
                content = content[:500] + f"\n[...{len(content) - 500} chars truncated...]"
            parts.append(f"[{ts}] {role}: {content}")
        return "\n\n".join(parts)

    # -- Pass 2: Curate --------------------------------------------------------

    def _pass_curate(self) -> dict:
        """Reorganize episodic memories + extract notes."""
        stats = {"curated": 0, "notes_extracted": 0, "deleted": 0, "merged": 0}

        # Get all archival entries
        try:
            table = self.archival._get_table()
            all_entries = table.to_pandas().to_dict("records")
        except Exception as e:
            logger.error("Curator: failed to read archival: %s", e)
            return stats

        entries = [e for e in all_entries if e.get("id") != "_init_"]
        if not entries:
            logger.info("Curator: no episodic memories to curate")
            return stats

        logger.info("Curator: curating %d episodic memories", len(entries))

        # Step 1: Reorganize with main model
        reorg_stats = self._curate_reorganize(entries)
        stats["curated"] = reorg_stats.get("curated", 0)
        stats["deleted"] = reorg_stats.get("deleted", 0)
        stats["merged"] = reorg_stats.get("merged", 0)

        # Re-read after reorganization (entries may have been deleted/merged)
        try:
            table = self.archival._get_table()
            entries = [
                e for e in table.to_pandas().to_dict("records")
                if e.get("id") != "_init_"
            ]
        except Exception:
            entries = []

        # Step 2: Extract notes with aux model
        if entries:
            extract_stats = self._curate_extract_notes(entries)
            stats["notes_extracted"] = extract_stats.get("notes_extracted", 0)

        self._stats["total_curated"] += stats["curated"]
        self._stats["total_deleted"] += stats["deleted"]
        self._stats["total_merged"] += stats["merged"]
        self._stats["total_notes_extracted"] += stats["notes_extracted"]
        return stats

    def _curate_reorganize(self, entries: list[dict]) -> dict:
        """Reorganize memories: deduplicate, merge, retag. Uses main model."""
        model = _get_model("main")
        if not model:
            return {"curated": 0, "deleted": 0, "merged": 0}

        stats = {"curated": 0, "deleted": 0, "merged": 0}

        for batch_start in range(0, len(entries), CURATE_BATCH_SIZE):
            batch = entries[batch_start:batch_start + CURATE_BATCH_SIZE]
            formatted = self._format_memories(batch)

            try:
                raw = _llm_call(CURATE_PROMPT, formatted, model, max_tokens=4000)
                result = _parse_json(raw)
                if not isinstance(result, dict):
                    continue

                actions = result.get("actions", [])
                self._apply_curate_actions(actions, {e["id"]: e for e in batch}, stats)

            except Exception as e:
                logger.error("Curator: reorganize batch failed: %s", e)

        return stats

    def _curate_extract_notes(self, entries: list[dict]) -> dict:
        """Extract reusable knowledge into notes. Uses aux model."""
        model = _get_model("aux")
        if not model:
            return {"notes_extracted": 0}

        # Collect existing tags for consistency
        existing_tags = set()
        for note in self.notes.list_notes():
            for tag in note.get("tags", []):
                existing_tags.add(tag.lower().strip())

        stats = {"notes_extracted": 0}

        for batch_start in range(0, len(entries), CURATE_BATCH_SIZE):
            batch = entries[batch_start:batch_start + CURATE_BATCH_SIZE]
            formatted = self._format_memories(batch)

            tag_hint = ""
            if existing_tags:
                tag_hint = (
                    f"\n\nExisting note tags (reuse when applicable): "
                    f"{', '.join(sorted(existing_tags))}"
                )

            try:
                raw = _llm_call(
                    EXTRACT_NOTES_PROMPT + tag_hint,
                    formatted,
                    model,
                    max_tokens=3000,
                )
                extractions = _parse_json(raw)
                if not isinstance(extractions, list):
                    continue

                for ext in extractions:
                    title = ext.get("title", "").strip()
                    content = ext.get("content", "").strip()
                    if not title or not content:
                        continue

                    tags = ext.get("tags", [])
                    if not isinstance(tags, list):
                        tags = []
                    tags = [t.lower().strip() for t in tags if isinstance(t, str)]
                    tags = normalize_tags(tags, existing_tags)

                    try:
                        filepath = self.notes.create(title, content, tags)
                        logger.info("Curator: created note '%s' -> %s", title, filepath)
                        stats["notes_extracted"] += 1
                        existing_tags.update(tags)
                    except Exception as e:
                        logger.error("Curator: failed to create note '%s': %s", title, e)
                        continue

                    # Remove source memory if fully extracted
                    source_id = ext.get("source_id", "")
                    if ext.get("remove_from_source") and source_id:
                        try:
                            table = self.archival._get_table()
                            table.delete(f'id = "{source_id}"')
                        except Exception as e:
                            logger.warning("Curator: failed to delete source %s: %s", source_id, e)

            except Exception as e:
                logger.error("Curator: note extraction batch failed: %s", e)

        return stats

    def _format_memories(self, entries: list[dict]) -> str:
        parts = []
        for e in entries:
            text = e.get("text", "")
            if len(text) > 800:
                text = text[:700] + f"\n[...{len(text) - 700} chars truncated...]"
            tags_raw = e.get("tags", "[]")
            if isinstance(tags_raw, str):
                try:
                    tags = json.loads(tags_raw)
                except Exception:
                    tags = []
            else:
                tags = tags_raw or []
            created = str(e.get("created_at", ""))[:10]
            mem_id = e.get("id", "?")
            tag_str = ", ".join(tags) if tags else "none"
            parts.append(f"--- [{mem_id}] [{created}] tags=[{tag_str}] ---\n{text}")
        return "\n\n".join(parts)

    def _apply_curate_actions(
        self, actions: list[dict], entries_by_id: dict, stats: dict
    ):
        table = self.archival._get_table()

        for action in actions:
            mem_id = action.get("id", "")
            act = action.get("action", "keep")

            if act == "keep":
                stats["curated"] += 1
                continue

            if act == "delete":
                try:
                    table.delete(f'id = "{mem_id}"')
                    stats["deleted"] += 1
                    logger.info("Curator: deleted %s — %s", mem_id, action.get("reason", ""))
                except Exception as e:
                    logger.warning("Curator: failed to delete %s: %s", mem_id, e)
                continue

            if act == "update":
                new_text = action.get("text", "")
                new_tags = action.get("tags", [])
                if new_text and mem_id in entries_by_id:
                    try:
                        table.delete(f'id = "{mem_id}"')
                        self.archival.add(
                            text=new_text,
                            metadata={"source": "curator_update"},
                            tags=new_tags if isinstance(new_tags, list) else [],
                        )
                        stats["curated"] += 1
                    except Exception as e:
                        logger.warning("Curator: failed to update %s: %s", mem_id, e)
                continue

            if act == "merge_into":
                target_id = action.get("target", "")
                if target_id and target_id in entries_by_id:
                    try:
                        table.delete(f'id = "{mem_id}"')
                        stats["merged"] += 1
                    except Exception as e:
                        logger.warning("Curator: failed to merge %s: %s", mem_id, e)
                continue

    # -- Logging ---------------------------------------------------------------

    def _log_run(self, result: dict):
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = (
                f"\n## Curator Run — {now}\n"
                f"- Harvested: {result.get('harvested', 0)}\n"
                f"- Curated: {result.get('curated', 0)}\n"
                f"- Merged: {result.get('merged', 0)}\n"
                f"- Deleted: {result.get('deleted', 0)}\n"
                f"- Notes extracted: {result.get('notes_extracted', 0)}\n"
                f"- Elapsed: {result.get('elapsed_s', 0)}s\n"
                f"---\n"
            )
            with open(LOG_PATH, "a") as f:
                f.write(entry)
        except Exception as e:
            logger.warning("Curator: failed to write log: %s", e)

    def get_status(self) -> str:
        """Compact status string for monitoring."""
        return (
            f"Curator | runs={self._stats['total_runs']} "
            f"harvested={self._stats['total_harvested']} "
            f"notes={self._stats['total_notes_extracted']} "
            f"deleted={self._stats['total_deleted']} "
            f"merged={self._stats['total_merged']} | "
            f"last: {self._last_run_ts or 'never'}"
        )


def run_curator(
    note_store: NoteStore,
    archival_memory,
    message_history,
    force: bool = False,
) -> dict:
    """Convenience function to run the curator (used by agent startup + heartbeat)."""
    curator = MemoryCurator(note_store, archival_memory, message_history)
    if not force and not curator.should_run():
        logger.info("Curator: skipping, last run too recent")
        return {"skipped": True}
    return curator.run()
