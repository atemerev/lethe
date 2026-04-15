"""Notes system — persistent procedural knowledge and conventions.

Notes are markdown files in ~/lethe/notes/ with YAML frontmatter.
They're indexed in lancedb for vector + FTS search and integrated
into hippocampus recall.

Common tags:
- skill: procedure for an external system that required discovery
- convention: how things should be done (user preferences, toolchain choices)
- (freeform): any other tag that fits (debugging, workaround, architecture, etc.)
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import lancedb
from lancedb.embeddings import get_registry

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
TABLE_NAME = "notes"

# Default notes directory
DEFAULT_NOTES_DIR = os.path.expanduser("~/lethe/notes")


def _slugify(title: str) -> str:
    """Convert title to a filesystem-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '_', slug)
    slug = slug.strip('_')
    return slug[:80] or "untitled"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter from markdown. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    header = text[3:end].strip()
    body = text[end + 4:].strip()

    meta = {}
    for line in header.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # Parse list values: [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            meta[key] = items
        else:
            meta[key] = val
    return meta, body


def _render_frontmatter(meta: dict) -> str:
    """Render metadata as YAML-ish frontmatter."""
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            items = ", ".join(val)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


class NoteStore:
    """Persistent notes with vector + FTS search via lancedb.

    Notes are stored as markdown files and indexed for search.
    The index can be rebuilt from files at any time (files are source of truth).
    """

    def __init__(self, db: lancedb.DBConnection, notes_dir: str = DEFAULT_NOTES_DIR):
        self.db = db
        self.notes_dir = Path(notes_dir)
        self.notes_dir.mkdir(parents=True, exist_ok=True)

        self.embedder = get_registry().get("sentence-transformers").create(
            name=EMBEDDING_MODEL
        )
        self._ensure_table()
        logger.info(f"NoteStore initialized: {self.notes_dir} ({self.count()} notes indexed)")

    def _ensure_table(self):
        """Create the notes table if it doesn't exist."""
        if TABLE_NAME not in self.db.table_names():
            init_vector = [0.0] * EMBEDDING_DIM
            self.db.create_table(
                TABLE_NAME,
                data=[{
                    "id": "_init_",
                    "title": "",
                    "text": "",
                    "tags": "[]",
                    "file_path": "",
                    "vector": init_vector,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }],
            )
            table = self.db.open_table(TABLE_NAME)
            try:
                table.create_fts_index("text", replace=True)
            except Exception as e:
                logger.warning(f"FTS index creation failed (non-fatal): {e}")
            logger.info(f"Created notes table")

    def _get_table(self):
        return self.db.open_table(TABLE_NAME)

    def _embed(self, text: str) -> list[float]:
        return self.embedder.compute_query_embeddings(text)[0]

    def count(self) -> int:
        try:
            return self._get_table().count_rows()
        except Exception:
            return 0

    def create(
        self,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Create a new note. Saves markdown file and indexes it.

        Args:
            title: Note title
            content: Note body (markdown)
            tags: List of tags (e.g. ["skill", "email", "graph-api"])

        Returns:
            File path of the created note
        """
        tags = tags or []
        now = datetime.now(timezone.utc)
        slug = _slugify(title)
        filename = f"{slug}.md"
        filepath = self.notes_dir / filename

        # Avoid overwriting — append number if exists
        counter = 1
        while filepath.exists():
            counter += 1
            filepath = self.notes_dir / f"{slug}_{counter}.md"

        meta = {
            "title": title,
            "tags": tags,
            "created": now.strftime("%Y-%m-%d"),
            "updated": now.strftime("%Y-%m-%d"),
        }
        file_content = f"{_render_frontmatter(meta)}\n\n{content}\n"
        filepath.write_text(file_content)

        # Index in lancedb
        note_id = f"note-{uuid.uuid4()}"
        # Embed title + tags + content for search
        search_text = f"{title}\n{' '.join(tags)}\n{content}"
        vector = self._embed(search_text)

        table = self._get_table()
        table.add([{
            "id": note_id,
            "title": title,
            "text": search_text,
            "tags": json.dumps(tags),
            "file_path": str(filepath),
            "vector": vector,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }])

        logger.info(f"Created note: {filepath} (tags: {tags})")
        return str(filepath)

    def search(
        self,
        query: str,
        tags: Optional[list[str]] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Search notes with vector similarity + optional tag filter.

        Args:
            query: Search query (natural language)
            tags: Optional tag filter (notes must have ALL specified tags)
            limit: Max results

        Returns:
            List of matching notes with score, title, tags, file_path, preview
        """
        table = self._get_table()
        vec = self._embed(query)
        results = table.search(vec).limit(limit * 3).to_list()

        notes = []
        for r in results:
            if r["id"] == "_init_":
                continue
            note_tags = json.loads(r.get("tags", "[]")) if isinstance(r.get("tags"), str) else r.get("tags", [])

            # Tag filter: note must contain all requested tags
            if tags and not all(t in note_tags for t in tags):
                continue

            filepath = r.get("file_path", "")
            # Read current file content for preview
            preview = ""
            if filepath and os.path.exists(filepath):
                raw = Path(filepath).read_text()
                _, body = _parse_frontmatter(raw)
                preview = body[:300]

            distance = r.get("_distance")
            score = 1.0 / (1.0 + max(0.0, float(distance))) if isinstance(distance, (int, float)) else 0.5

            notes.append({
                "title": r.get("title", ""),
                "tags": note_tags,
                "file_path": filepath,
                "preview": preview,
                "score": score,
                "created_at": r.get("created_at", ""),
            })

            if len(notes) >= limit:
                break

        return notes

    def list_notes(self, tags: Optional[list[str]] = None) -> list[dict]:
        """List all notes, optionally filtered by tags.

        Returns:
            List of notes with title, tags, file_path
        """
        notes = []
        for filepath in sorted(self.notes_dir.glob("*.md")):
            raw = filepath.read_text()
            meta, body = _parse_frontmatter(raw)
            note_tags = meta.get("tags", [])
            if isinstance(note_tags, str):
                note_tags = [note_tags]

            if tags and not all(t in note_tags for t in tags):
                continue

            notes.append({
                "title": meta.get("title", filepath.stem),
                "tags": note_tags,
                "file_path": str(filepath),
                "created": meta.get("created", ""),
            })
        return notes

    def reindex(self) -> int:
        """Rebuild the lancedb index from note files on disk.

        Returns:
            Number of notes indexed
        """
        # Drop and recreate table
        try:
            self.db.drop_table(TABLE_NAME)
        except Exception:
            pass
        self._ensure_table()

        table = self._get_table()
        count = 0
        now = datetime.now(timezone.utc).isoformat()

        for filepath in self.notes_dir.glob("*.md"):
            raw = filepath.read_text()
            meta, body = _parse_frontmatter(raw)
            title = meta.get("title", filepath.stem)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]

            search_text = f"{title}\n{' '.join(tags)}\n{body}"
            vector = self._embed(search_text)

            table.add([{
                "id": f"note-{uuid.uuid4()}",
                "title": title,
                "text": search_text,
                "tags": json.dumps(tags),
                "file_path": str(filepath),
                "vector": vector,
                "created_at": meta.get("created", now),
                "updated_at": meta.get("updated", now),
            }])
            count += 1

        # Rebuild FTS index
        try:
            table.create_fts_index("text", replace=True)
        except Exception as e:
            logger.warning(f"FTS index rebuild failed: {e}")

        logger.info(f"Reindexed {count} notes")
        return count
