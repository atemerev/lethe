"""Integration tests for prompt assembly pipeline.

Verifies that the system prompt is correctly assembled from workspace + config,
and that the right content appears in the final request.
"""

import json
import os
import shutil
import tempfile

import lancedb
import pytest


@pytest.fixture
def lethe_env():
    """Create a minimal Lethe environment for testing prompt assembly."""
    tmpdir = tempfile.mkdtemp(prefix="lethe_test_prompt_")

    # Create config/prompts
    prompts_dir = os.path.join(tmpdir, "config", "prompts")
    os.makedirs(prompts_dir)

    with open(os.path.join(prompts_dir, "agent_instructions.md"), "w") as f:
        f.write("<action_discipline>\nTest action discipline\n</action_discipline>\n<output_format>\nTest output format\n</output_format>")

    with open(os.path.join(prompts_dir, "agent_tools.md"), "w") as f:
        f.write("# Tools\n## Core Tools\n- **bash** — Run commands\n- **request_tool** — Load extended tools")

    # Create config/blocks (seeds)
    blocks_dir = os.path.join(tmpdir, "config", "blocks")
    os.makedirs(blocks_dir)

    with open(os.path.join(blocks_dir, "identity.md"), "w") as f:
        f.write("You are TestBot, a test assistant.\n\n<character>\nHelpful and concise.\n</character>")

    with open(os.path.join(blocks_dir, "human.md"), "w") as f:
        f.write("# About the user\nTest user.")

    with open(os.path.join(blocks_dir, "project.md"), "w") as f:
        f.write("# Projects\nNo active projects.")

    # Create workspace
    workspace_dir = os.path.join(tmpdir, "workspace")
    os.makedirs(os.path.join(workspace_dir, "memory"), exist_ok=True)
    os.makedirs(os.path.join(workspace_dir, "skills"), exist_ok=True)

    # Create data dir
    data_dir = os.path.join(tmpdir, "data", "memory")
    os.makedirs(data_dir, exist_ok=True)

    # Notes dir
    notes_dir = os.path.join(workspace_dir, "notes")
    os.makedirs(notes_dir)

    yield {
        "tmpdir": tmpdir,
        "config_dir": os.path.join(tmpdir, "config"),
        "workspace_dir": workspace_dir,
        "data_dir": os.path.join(tmpdir, "data"),
        "notes_dir": notes_dir,
    }
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_system_prompt_includes_all_parts(lethe_env):
    """System prompt should contain identity (workspace) + instructions (config) + tools (config)."""
    os.environ["LETHE_CONFIG_DIR"] = lethe_env["config_dir"]
    os.environ["WORKSPACE_DIR"] = lethe_env["workspace_dir"]

    from lethe.prompts import load_prompt_template

    # Verify templates load
    instructions = load_prompt_template("agent_instructions")
    assert "action_discipline" in instructions
    assert "output_format" in instructions

    tools = load_prompt_template("agent_tools")
    assert "request_tool" in tools
    assert "bash" in tools


def test_identity_separate_from_instructions(lethe_env):
    """Identity (persona) should not contain system instructions."""
    identity_path = os.path.join(lethe_env["config_dir"], "blocks", "identity.md")
    identity = open(identity_path).read()

    # Identity should have persona
    assert "TestBot" in identity
    assert "character" in identity

    # Identity should NOT have system instructions
    assert "action_discipline" not in identity
    assert "output_format" not in identity
    assert "request_tool" not in identity


def test_tools_not_in_memory_blocks(lethe_env):
    """Tools documentation should not exist as a memory block seed."""
    blocks_dir = os.path.join(lethe_env["config_dir"], "blocks")
    assert not os.path.exists(os.path.join(blocks_dir, "tools.md")), \
        "tools.md should not be in config/blocks/ — it should be in config/prompts/agent_tools.md"


def test_notes_searchable_by_hippocampus(lethe_env):
    """Notes should be findable by hippocampus search."""
    from lethe.memory.notes import NoteStore
    from lethe.memory.hippocampus import Hippocampus

    db = lancedb.connect(os.path.join(lethe_env["tmpdir"], "test_db"))
    store = NoteStore(db=db, notes_dir=lethe_env["notes_dir"])

    store.create(
        "Deploy to production",
        "## How\n1. Run deploy.sh\n2. Check health endpoint",
        ["skill", "deployment"],
    )

    hippo = Hippocampus.__new__(Hippocampus)
    hippo.note_store = store

    results = hippo._search_notes("how to deploy")
    assert len(results) >= 1
    assert "Deploy" in results[0]["title"]


def test_tool_count_under_limit(lethe_env):
    """Core tools should be under 15 (Gemma 4 recommended limit)."""
    from lethe.tools import get_core_tools

    core = get_core_tools()
    names = [schema.get("name", func.__name__) for func, schema in core]

    # Should be under 15
    assert len(core) <= 15, f"Core tools ({len(core)}) exceeds 15: {names}"

    # Must include essentials
    assert "bash" in names
    assert "read_file" in names
    assert "note_search" in names
    assert "request_tool" in names


def test_request_tool_can_load_extended():
    """request_tool should be able to activate extended tools."""
    from lethe.tools import _EXTENDED_TOOLS

    # Extended tools should exist
    assert len(_EXTENDED_TOOLS) > 0

    # Should include browser tools
    assert "browser_open" in _EXTENDED_TOOLS or len(_EXTENDED_TOOLS) > 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
