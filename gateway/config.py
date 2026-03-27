"""Gateway configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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
