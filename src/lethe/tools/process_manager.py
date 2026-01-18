"""Process manager for tracking background shell processes."""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BackgroundProcess:
    """Tracks a background shell process."""
    process: subprocess.Popen
    command: str
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    status: str = "running"  # running, completed, failed
    exit_code: Optional[int] = None
    start_time: Optional[datetime] = None


# Global registry of background processes
background_processes: dict[str, BackgroundProcess] = {}

# Counter for generating unique bash IDs
_bash_id_counter = 0


def get_next_bash_id() -> str:
    """Get the next unique bash ID."""
    global _bash_id_counter
    _bash_id_counter += 1
    return f"bash_{_bash_id_counter}"


def get_process(shell_id: str) -> Optional[BackgroundProcess]:
    """Get a background process by ID."""
    return background_processes.get(shell_id)


def register_process(shell_id: str, proc: BackgroundProcess):
    """Register a new background process."""
    background_processes[shell_id] = proc


def remove_process(shell_id: str) -> bool:
    """Remove a background process from tracking."""
    if shell_id in background_processes:
        del background_processes[shell_id]
        return True
    return False


def list_processes() -> dict[str, BackgroundProcess]:
    """List all tracked background processes."""
    return background_processes.copy()
