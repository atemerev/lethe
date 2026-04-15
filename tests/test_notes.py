"""Test notes system — create, search, hippocampus recall."""

import os
import shutil
import tempfile

import lancedb
import pytest


@pytest.fixture
def note_env():
    """Create a temp directory and lancedb for testing."""
    tmpdir = tempfile.mkdtemp(prefix="lethe_test_notes_")
    notes_dir = os.path.join(tmpdir, "notes")
    db_dir = os.path.join(tmpdir, "lancedb")
    db = lancedb.connect(db_dir)
    yield notes_dir, db
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_create_and_search(note_env):
    """Create a note and find it via search."""
    from lethe.memory.notes import NoteStore

    notes_dir, db = note_env
    store = NoteStore(db=db, notes_dir=notes_dir)

    # Create a skill note
    path = store.create(
        title="Read UNIGE email via Microsoft Graph API",
        content="## What\nAccess UNIGE Outlook email programmatically.\n\n## How\n1. Token at ~/.local/opt/davmail/graph_tokens.json\n2. Refresh via MSAL\n3. curl with Bearer token",
        tags=["skill", "email", "graph-api"],
    )
    assert os.path.exists(path)
    assert path.endswith(".md")

    # Create a convention note
    store.create(
        title="Use uv for Python package management",
        content="Use `uv pip install` not `pip install`. Principal's standard toolchain.",
        tags=["convention", "python", "tooling"],
    )

    # Search for graph api
    results = store.search("how to read email with graph api")
    assert len(results) >= 1
    assert "Graph API" in results[0]["title"]
    assert "skill" in results[0]["tags"]

    # Search for python convention
    results = store.search("how to install python packages")
    assert len(results) >= 1
    assert any("uv" in r["title"].lower() for r in results)

    # Search with tag filter
    results = store.search("email graph api", tags=["skill"])
    assert len(results) >= 1
    assert "Graph API" in results[0]["title"]

    # List all
    all_notes = store.list_notes()
    assert len(all_notes) == 2

    # List by tag
    skills = store.list_notes(tags=["skill"])
    assert len(skills) == 1
    conventions = store.list_notes(tags=["convention"])
    assert len(conventions) == 1


def test_reindex(note_env):
    """Reindex rebuilds from files on disk."""
    from lethe.memory.notes import NoteStore

    notes_dir, db = note_env
    store = NoteStore(db=db, notes_dir=notes_dir)

    store.create("Test note", "Some content", ["test"])
    assert store.count() >= 2  # 1 note + _init_ row

    # Reindex
    count = store.reindex()
    assert count == 1  # 1 file on disk

    # Search still works
    results = store.search("test")
    assert len(results) >= 1


def test_hippocampus_finds_notes(note_env):
    """Hippocampus _search_notes returns matching notes."""
    from lethe.memory.notes import NoteStore
    from lethe.memory.hippocampus import Hippocampus

    notes_dir, db = note_env
    store = NoteStore(db=db, notes_dir=notes_dir)

    store.create(
        title="Read UNIGE email via Graph API",
        content="Token at graph_tokens.json, refresh via MSAL, curl with Bearer",
        tags=["skill", "email"],
    )

    # Create a minimal hippocampus with note_store set
    hippo = Hippocampus.__new__(Hippocampus)
    hippo.note_store = store

    results = hippo._search_notes("how to access email")
    assert len(results) >= 1
    assert "Graph API" in results[0]["title"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
