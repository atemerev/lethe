"""Gateway configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_or_worker_file(name: str, env_file: str, default: str = "") -> str:
    """Read a value from the process environment, then the worker env file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value

    try:
        env_path = Path(env_file)
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                if key.strip() == name:
                    return raw_value.strip()
    except Exception:
        pass
    return default


@dataclass
class GatewayConfig:
    """Configuration for the gateway bot."""

    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    pool_size: int = field(
        default_factory=lambda: int(os.environ.get("POOL_SIZE", "3"))
    )
    lethe_image: str = field(
        default_factory=lambda: os.environ.get("LETHE_IMAGE", "lethe:latest")
    )
    workspace_base: str = field(
        default_factory=lambda: os.environ.get("WORKSPACE_BASE", "/data/lethe-users")
    )
    container_env_file: str = field(
        default_factory=lambda: os.environ.get("CONTAINER_ENV_FILE", "/etc/lethe/worker.env")
    )
    worker_api_token: str = field(
        default_factory=lambda: _env_or_worker_file(
            "LETHE_API_TOKEN",
            os.environ.get("CONTAINER_ENV_FILE", "/etc/lethe/worker.env"),
        )
    )
    state_file: str = field(
        default_factory=lambda: os.environ.get("STATE_FILE", "/data/gateway/state.json")
    )
    port_range_start: int = field(
        default_factory=lambda: int(os.environ.get("PORT_RANGE_START", "9000"))
    )
    idle_timeout_days: int = field(
        default_factory=lambda: int(os.environ.get("IDLE_TIMEOUT_DAYS", "7"))
    )
    # Docker network for worker containers
    network_name: str = field(
        default_factory=lambda: os.environ.get("DOCKER_NETWORK", "lethe-net")
    )
