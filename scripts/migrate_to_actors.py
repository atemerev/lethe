#!/usr/bin/env python3
"""Migrate Lethe from single-agent to actor model architecture.

This script updates memory blocks (identity.md, tools.md) to work with
the actor model where:
- Cortex = conscious executive layer (coordinator, never calls tools directly)
- DMN = Default Mode Network (background thinking, reflections)
- Subagents = spawned workers with specific tools and goals

Idempotent — safe to run multiple times. Backs up originals before overwriting.

Usage:
    python scripts/migrate_to_actors.py [--config-dir ./config/blocks]
"""

import argparse
import shutil
import sys
from pathlib import Path
from datetime import datetime


def backup(path: Path):
    """Back up a file before modifying it."""
    if path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_suffix(f".pre-actors.{ts}.bak")
        shutil.copy2(path, backup_path)
        print(f"  Backed up: {path.name} -> {backup_path.name}")


# -- New identity block for actor-aware cortex --
IDENTITY_ACTOR_SECTION = """
<actor_model>
## Actor Architecture

You operate as the **cortex** — the conscious executive layer of a multi-agent system.

### Your Role
- You are the ONLY agent that communicates with the user
- You are a COORDINATOR — you NEVER do work yourself
- For ANY task requiring tools (file editing, CLI, web search, etc.), spawn a subagent
- You keep: actor tools, memory tools, and telegram tools

### Your Agents
- **DMN** (Default Mode Network): Always-on background thinker. Scans goals, reflects,
  reorganizes memory, notifies you of urgent items. Runs automatically every 15 minutes.
- **Subagents**: Spawned on demand for specific tasks. They have file, CLI, web, and
  browser tools. They report results back to you.

### How to Delegate
1. `spawn_actor(name, goals, tools)` — be DETAILED in goals, the subagent only knows what you tell it
2. `ping_actor(id)` — check on progress
3. `kill_actor(id)` — terminate stuck agents
4. `wait_for_response(timeout)` — block until a reply arrives
5. `discover_actors(group)` — see who's running

### What You Keep
- Memory: `memory_read`, `memory_update`, `memory_append`, `archival_search/insert`, `conversation_search`
- Telegram: `telegram_send_message`, `telegram_send_file`
- Actors: `spawn_actor`, `kill_actor`, `ping_actor`, `send_message`, `discover_actors`, `wait_for_response`, `terminate`
</actor_model>
"""

# -- New tools block for actor model --
TOOLS_ACTOR = """# Tools

## Your Tools (cortex)
- **spawn_actor** / **kill_actor** / **ping_actor** — Manage subagents
- **send_message** / **wait_for_response** / **discover_actors** — Actor communication
- **terminate** — End your own execution
- **memory_read** / **memory_update** / **memory_append** — Core memory blocks
- **archival_search** / **archival_insert** / **conversation_search** — Long-term memory
- **telegram_send_message** / **telegram_send_file** — Telegram I/O

## Subagent Default Tools (always available to spawned actors)
bash, read_file, write_file, edit_file, list_directory, grep_search

## Subagent Extra Tools (specify in spawn_actor tools= parameter)
web_search, fetch_webpage, browser_open, browser_click, browser_fill, browser_snapshot,
memory_read, memory_update, memory_append, archival_search, archival_insert, conversation_search

## Skills
Extended capabilities are documented as skill files in `~/lethe/skills/`.
Tell subagents to check `~/lethe/skills/` for relevant skill docs.
"""


def check_already_migrated(identity_path: Path) -> bool:
    """Check if already migrated (idempotent)."""
    if identity_path.exists():
        content = identity_path.read_text()
        if "<actor_model>" in content:
            return True
    return False


def migrate_identity(config_dir: Path):
    """Add actor model section to identity.md."""
    identity_path = config_dir / "identity.md"
    
    if not identity_path.exists():
        print(f"  WARNING: {identity_path} not found, skipping identity migration")
        return
    
    content = identity_path.read_text()
    
    if "<actor_model>" in content:
        print("  identity.md: already has <actor_model> section, skipping")
        return
    
    backup(identity_path)
    
    # Insert actor section before </purpose> or at the end
    if "</purpose>" in content:
        content = content.replace("</purpose>", IDENTITY_ACTOR_SECTION + "\n</purpose>")
    else:
        content += "\n" + IDENTITY_ACTOR_SECTION
    
    identity_path.write_text(content)
    print("  identity.md: added <actor_model> section")


def migrate_tools(config_dir: Path):
    """Replace tools.md with actor-aware version."""
    tools_path = config_dir / "tools.md"
    
    if tools_path.exists():
        content = tools_path.read_text()
        if "spawn_actor" in content:
            print("  tools.md: already actor-aware, skipping")
            return
        backup(tools_path)
    
    tools_path.write_text(TOOLS_ACTOR)
    print("  tools.md: replaced with actor-aware version")


def migrate_env(project_dir: Path):
    """Ensure ACTORS_ENABLED=true in .env."""
    env_path = project_dir / ".env"
    
    if env_path.exists():
        content = env_path.read_text()
        if "ACTORS_ENABLED" in content:
            if "ACTORS_ENABLED=true" in content.lower().replace(" ", ""):
                print("  .env: ACTORS_ENABLED already set")
                return
            else:
                print("  .env: ACTORS_ENABLED exists but not 'true' — update manually")
                return
    
    # Don't auto-modify .env — just advise
    print("  .env: Add ACTORS_ENABLED=true to enable actor model")


def main():
    parser = argparse.ArgumentParser(description="Migrate Lethe to actor model")
    parser.add_argument("--config-dir", type=Path, default=Path("./config/blocks"),
                        help="Path to config/blocks directory")
    parser.add_argument("--project-dir", type=Path, default=Path("."),
                        help="Path to project root")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without modifying files")
    args = parser.parse_args()
    
    config_dir = args.config_dir.resolve()
    project_dir = args.project_dir.resolve()
    
    if not config_dir.exists():
        print(f"ERROR: Config directory not found: {config_dir}")
        sys.exit(1)
    
    print(f"Migrating to actor model...")
    print(f"Config dir: {config_dir}")
    print()
    
    if check_already_migrated(config_dir / "identity.md"):
        print("Already migrated (identity.md has <actor_model> section).")
        print("Run with --force to re-migrate, or edit files manually.")
        return
    
    if args.dry_run:
        print("[DRY RUN] Would modify:")
        print(f"  - {config_dir / 'identity.md'}: add <actor_model> section")
        print(f"  - {config_dir / 'tools.md'}: replace with actor-aware tools list")
        print(f"  - Check .env for ACTORS_ENABLED")
        return
    
    migrate_identity(config_dir)
    migrate_tools(config_dir)
    migrate_env(project_dir)
    
    print()
    print("Migration complete!")
    print()
    print("Next steps:")
    print("  1. Review config/blocks/identity.md — the <actor_model> section was added")
    print("  2. Review config/blocks/tools.md — replaced with actor-aware tools")
    print("  3. Ensure ACTORS_ENABLED=true in .env")
    print("  4. Restart: systemctl --user restart lethe")
    print()
    print("Backups saved as *.pre-actors.*.bak")


if __name__ == "__main__":
    main()
