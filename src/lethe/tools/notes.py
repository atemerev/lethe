"""Note management tools — search, create, and list persistent notes.

Notes are markdown files in $LETHE_HOME/workspace/notes/ with tags for categorization.
Common tags: skill (procedures), convention (preferences), plus freeform.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# NoteStore instance — set by agent at init time
_store = None


def set_store(store):
    """Set the NoteStore instance for tools to use."""
    global _store
    _store = store


def _is_tool(func):
    func._is_tool = True
    return func


@_is_tool
def note_search(query: str, tags: str = "") -> str:
    """Search persistent notes (skills, conventions, procedures).

    Notes contain reusable knowledge: how to access APIs, user preferences,
    setup procedures, conventions. Search here when you need to recall
    how something was done before.

    Args:
        query: Search query (natural language)
        tags: Optional comma-separated tag filter (e.g. "skill,email")

    Returns:
        Matching notes with titles, tags, and content previews
    """
    if not _store:
        return "Notes system not initialized."

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    results = _store.search(query, tags=tag_list, limit=5)

    if not results:
        return f"No notes found for: {query}" + (f" (tags: {tags})" if tags else "")

    output = []
    for r in results:
        tag_str = ", ".join(r["tags"]) if r["tags"] else "none"
        preview = r["preview"].replace("\n", " ")[:200] if r["preview"] else ""
        output.append(
            f"**{r['title']}** [{tag_str}]\n"
            f"  File: {r['file_path']}\n"
            f"  {preview}"
        )

    return f"Found {len(results)} notes:\n\n" + "\n\n".join(output)


@_is_tool
def note_create(title: str, content: str, tags: str = "") -> str:
    """Create a persistent note (skill, convention, or general knowledge).

    Use this to save reusable knowledge:
    - Skills: procedures for external systems (APIs, services)
    - Conventions: user preferences ("use uv not pip")
    - Any other knowledge worth persisting across sessions

    Args:
        title: Short descriptive title
        content: Note body in markdown (## What, ## How, ## Key files)
        tags: Comma-separated tags (e.g. "skill,email,graph-api")

    Returns:
        Confirmation with file path
    """
    if not _store:
        return "Notes system not initialized."

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    filepath = _store.create(title, content, tag_list)
    return f"Note saved: {filepath} (tags: {', '.join(tag_list)})"


@_is_tool
def note_list(tags: str = "") -> str:
    """List all persistent notes, optionally filtered by tags.

    Args:
        tags: Optional comma-separated tag filter (e.g. "skill" or "convention")

    Returns:
        List of notes with titles and tags
    """
    if not _store:
        return "Notes system not initialized."

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    notes = _store.list_notes(tags=tag_list)

    if not notes:
        return "No notes found." + (f" (tags: {tags})" if tags else "")

    output = []
    for n in notes:
        tag_str = ", ".join(n["tags"]) if n["tags"] else "none"
        output.append(f"- **{n['title']}** [{tag_str}] — {n['file_path']}")

    return f"{len(notes)} notes:\n" + "\n".join(output)
