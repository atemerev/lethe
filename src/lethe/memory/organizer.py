"""Memory organizer — migrates valuable archival entries to notes, deletes noise.

Runs on startup. Walks all archival memory entries in batches, uses an LLM call
to evaluate each batch, creates notes for valuable entries, and deletes everything
processed from archival memory.

Over time, this drains archival memory into the notes system, keeping only
structured, searchable knowledge.
"""

import json
import logging
import os
from typing import Optional

from litellm import completion

from lethe.memory.notes import NoteStore
from lethe.prompts import load_prompt_template

logger = logging.getLogger(__name__)

EVALUATE_PROMPT = load_prompt_template(
    "organizer_evaluate",
    fallback="Evaluate these memory entries. Respond with a JSON array of {keep: bool, title?, tags?, content?} objects.",
)

# Process entries in batches to reduce LLM calls
BATCH_SIZE = 8


def _get_llm_config():
    """Get LLM config from environment."""
    model = os.environ.get("LLM_MODEL_AUX") or os.environ.get("LLM_MODEL", "")
    api_base = os.environ.get("LLM_API_BASE", "")
    return model, api_base


def _collect_existing_tags(note_store: NoteStore) -> set[str]:
    """Collect all tags currently used across notes."""
    tags = set()
    for note in note_store.list_notes():
        for tag in note.get("tags", []):
            tags.add(tag.lower().strip())
    return tags


def _normalize_tags(tags: list[str], existing_tags: set[str]) -> list[str]:
    """Normalize LLM-suggested tags against existing vocabulary.

    Handles common inconsistencies: plurals, case, hyphens vs underscores.
    If an existing tag is close enough, use it instead. All tags are lowercased.
    """
    normalized = []
    seen = set()
    for tag in tags:
        tag = tag.lower().strip()
        if not tag or tag in seen:
            continue
        # Exact match
        if tag in existing_tags:
            normalized.append(tag)
            seen.add(tag)
            continue
        # Try singular/plural variants
        if tag.endswith("s") and tag[:-1] in existing_tags:
            normalized.append(tag[:-1])
            seen.add(tag[:-1])
            continue
        if not tag.endswith("s") and tag + "s" in existing_tags:
            normalized.append(tag + "s")
            seen.add(tag + "s")
            continue
        # Try hyphen/underscore swap
        swapped = tag.replace("-", "_") if "-" in tag else tag.replace("_", "-")
        if swapped in existing_tags:
            normalized.append(swapped)
            seen.add(swapped)
            continue
        # New tag — use as-is
        normalized.append(tag)
        seen.add(tag)
    return normalized


def _format_batch(entries: list[dict]) -> str:
    """Format a batch of archival entries for the LLM prompt."""
    parts = []
    for i, entry in enumerate(entries):
        text = entry.get("text", "")
        # Cap each entry to avoid blowing the aux model's context
        if len(text) > 1000:
            text = text[:800] + f"\n[...{len(text) - 800} chars truncated...]"
        created = entry.get("created_at", "")[:10]
        parts.append(f"--- Entry {i} [{created}] ---\n{text}")
    return "\n\n".join(parts)


def _evaluate_batch(entries: list[dict], existing_tags: set[str]) -> list[dict]:
    """Ask the LLM to evaluate a batch of entries. Returns list of decisions."""
    model, api_base = _get_llm_config()
    if not model:
        logger.warning("Organizer: no LLM model configured, skipping")
        return [{"keep": False}] * len(entries)

    batch_text = _format_batch(entries)

    # Include existing tags in prompt so LLM reuses them
    tag_hint = ""
    if existing_tags:
        sorted_tags = sorted(existing_tags)
        tag_hint = f"\n\nExisting tags (reuse these when applicable, don't create synonyms): {', '.join(sorted_tags)}"

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": EVALUATE_PROMPT + tag_hint},
            {"role": "user", "content": f"Evaluate these {len(entries)} entries:\n\n{batch_text}"},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    if api_base:
        kwargs["api_base"] = api_base

    try:
        response = completion(**kwargs)
        content = response.choices[0].message.content or ""

        # Extract JSON array from response
        import re
        # Find the JSON array — handle markdown code fences
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r'^```\w*\n?', '', content)
            content = re.sub(r'\n?```$', '', content)
            content = content.strip()

        decisions = json.loads(content)
        if not isinstance(decisions, list):
            logger.warning(f"Organizer: expected list, got {type(decisions)}")
            return [{"keep": False}] * len(entries)

        # Pad/truncate to match entry count
        while len(decisions) < len(entries):
            decisions.append({"keep": False})
        return decisions[:len(entries)]

    except json.JSONDecodeError as e:
        logger.warning(f"Organizer: failed to parse LLM response as JSON: {e}")
        return [{"keep": False}] * len(entries)
    except Exception as e:
        logger.error(f"Organizer: LLM call failed: {e}")
        return [{"keep": False}] * len(entries)


def organize(note_store: NoteStore, archival_memory, dry_run: bool = False) -> dict:
    """Walk all archival entries, extract valuable ones to notes, delete the rest.

    Args:
        note_store: NoteStore instance to create notes in
        archival_memory: ArchivalMemory instance to read/delete from
        dry_run: If True, only evaluate and log — do NOT create notes or delete entries.
                 Safe to call with production data.

    Returns:
        Stats dict: {processed, kept, discarded, errors}
    """
    if dry_run:
        logger.info("Organizer: DRY RUN — no notes will be created, no entries deleted")

    # Get all archival entries (read-only snapshot)
    table = archival_memory._get_table()
    all_entries = table.to_pandas().to_dict("records")

    # Filter out init row
    entries = [e for e in all_entries if e.get("id") != "_init_"]

    if not entries:
        logger.info("Organizer: no archival entries to process")
        return {"processed": 0, "kept": 0, "discarded": 0, "errors": 0}

    logger.info(f"Organizer: processing {len(entries)} archival entries in batches of {BATCH_SIZE}")

    # Collect existing tags for consistency (updated as new notes are created)
    existing_tags = _collect_existing_tags(note_store)
    logger.info(f"Organizer: existing tag vocabulary ({len(existing_tags)}): {sorted(existing_tags)}")

    stats = {"processed": 0, "kept": 0, "discarded": 0, "errors": 0}
    ids_to_delete = []

    # Process in batches
    for batch_start in range(0, len(entries), BATCH_SIZE):
        batch = entries[batch_start:batch_start + BATCH_SIZE]
        decisions = _evaluate_batch(batch, existing_tags)

        for entry, decision in zip(batch, decisions):
            stats["processed"] += 1
            entry_id = entry.get("id", "")

            if decision.get("keep"):
                title = decision.get("title", "Untitled")
                tags = _normalize_tags(decision.get("tags", []), existing_tags)
                content = decision.get("content", "")

                if not content:
                    stats["discarded"] += 1
                    ids_to_delete.append(entry_id)
                    continue

                if not dry_run:
                    try:
                        filepath = note_store.create(title, content, tags)
                        logger.info(f"Organizer: created note '{title}' -> {filepath}")
                        stats["kept"] += 1
                        # Update tag vocabulary for subsequent batches
                        existing_tags.update(t.lower() for t in tags)
                    except Exception as e:
                        logger.error(f"Organizer: failed to create note '{title}': {e}")
                        stats["errors"] += 1
                        continue
                else:
                    logger.info(f"Organizer [dry-run]: would create note '{title}' (tags: {tags})")
                    stats["kept"] += 1
                    existing_tags.update(t.lower() for t in tags)
            else:
                stats["discarded"] += 1

            ids_to_delete.append(entry_id)

    # Delete processed entries from archival memory
    if ids_to_delete and not dry_run:
        try:
            table = archival_memory._get_table()
            for entry_id in ids_to_delete:
                try:
                    table.delete(f'id = "{entry_id}"')
                except Exception as e:
                    logger.warning(f"Organizer: failed to delete entry {entry_id}: {e}")
            logger.info(f"Organizer: deleted {len(ids_to_delete)} archival entries")
        except Exception as e:
            logger.error(f"Organizer: bulk delete failed: {e}")
            stats["errors"] += 1

    logger.info(
        f"Organizer: done — {stats['processed']} processed, "
        f"{stats['kept']} kept as notes, {stats['discarded']} discarded, "
        f"{stats['errors']} errors"
    )
    return stats
