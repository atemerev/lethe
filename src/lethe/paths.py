"""Canonical path resolution for all Lethe runtime directories.

All paths derive from LETHE_HOME (env var, default ~/.lethe).
Import-safe: no heavy dependencies, no circular imports.
"""

import os
from pathlib import Path


def lethe_home() -> Path:
    return Path(os.environ.get("LETHE_HOME", os.path.expanduser("~/.lethe")))


def workspace_dir() -> Path:
    return Path(os.environ.get("WORKSPACE_DIR", str(lethe_home() / "workspace")))


def data_dir() -> Path:
    return lethe_home() / "data"


def memory_dir() -> Path:
    return Path(os.environ.get("MEMORY_DIR", str(data_dir() / "memory")))


def db_path() -> Path:
    return Path(os.environ.get("DB_PATH", str(data_dir() / "lethe.db")))


def credentials_dir() -> Path:
    return lethe_home() / "credentials"


def cache_dir() -> Path:
    return lethe_home() / "cache"


def logs_dir() -> Path:
    return lethe_home() / "logs"


def notes_dir() -> Path:
    return workspace_dir() / "notes"


def config_dir() -> Path:
    """Seed config templates directory (in the repo, not under LETHE_HOME)."""
    return Path(os.environ.get("LETHE_CONFIG_DIR", "config"))
