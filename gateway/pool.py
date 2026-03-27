"""Container pool manager — maintains warm Lethe containers for instant assignment."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import docker
import httpx

from gateway.config import GatewayConfig

logger = logging.getLogger(__name__)


class ContainerState(str, Enum):
    WARMING = "warming"
    IDLE = "idle"
    ASSIGNED = "assigned"
    STOPPED = "stopped"


@dataclass
class ContainerInfo:
    container_id: str
    container_name: str
    state: str  # ContainerState value
    user_id: Optional[int] = None
    port: int = 0
    workspace_path: str = ""
    created_at: float = 0.0
    assigned_at: Optional[float] = None
    last_activity: Optional[float] = None

    @property
    def api_url(self) -> str:
        return f"http://localhost:{self.port}"


class PoolManager:
    """Manages a pool of pre-materialized Lethe containers."""

    LABEL = "lethe-worker"

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.docker = docker.from_env()
        self.containers: dict[str, ContainerInfo] = {}  # container_id -> info
        self.user_map: dict[int, str] = {}  # user_id -> container_id
        self._next_port = config.port_range_start
        self._lock = asyncio.Lock()

    async def start(self):
        """Initialize pool: recover existing containers, then ensure pool size."""
        self._ensure_network()
        self._recover_state()
        await self.ensure_pool()

    def _ensure_network(self):
        """Create Docker network if it doesn't exist."""
        try:
            self.docker.networks.get(self.config.network_name)
        except docker.errors.NotFound:
            self.docker.networks.create(self.config.network_name, driver="bridge")
            logger.info("Created Docker network: %s", self.config.network_name)

    def _recover_state(self):
        """Recover state from persisted file and running Docker containers."""
        state_path = Path(self.config.state_file)
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                self.user_map = {int(k): v for k, v in data.get("user_map", {}).items()}
                for info_dict in data.get("containers", []):
                    info = ContainerInfo(**info_dict)
                    self.containers[info.container_id] = info
                    if info.port >= self._next_port:
                        self._next_port = info.port + 1
                logger.info(
                    "Recovered state: %d containers, %d users",
                    len(self.containers),
                    len(self.user_map),
                )
            except Exception as e:
                logger.warning("Failed to load state file: %s", e)

        # Verify against Docker reality
        try:
            running = self.docker.containers.list(
                filters={"label": self.LABEL}, all=True
            )
        except Exception as e:
            logger.error("Failed to list Docker containers: %s", e)
            return

        running_ids = {c.id for c in running}

        # Remove containers that no longer exist in Docker
        stale = [cid for cid in self.containers if cid not in running_ids]
        for cid in stale:
            info = self.containers.pop(cid)
            if info.user_id and info.user_id in self.user_map:
                # Keep user mapping for workspace reuse, but clear container ref
                del self.user_map[info.user_id]
            logger.info("Removed stale container %s", cid[:12])

        self._save_state()

    def _save_state(self):
        """Persist state to disk."""
        state_path = Path(self.config.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "user_map": {str(k): v for k, v in self.user_map.items()},
            "containers": [asdict(info) for info in self.containers.values()],
        }
        state_path.write_text(json.dumps(data, indent=2))

    def _allocate_port(self) -> int:
        port = self._next_port
        self._next_port += 1
        return port

    async def ensure_pool(self):
        """Ensure we have enough idle containers in the pool."""
        async with self._lock:
            idle_count = sum(
                1 for c in self.containers.values() if c.state == ContainerState.IDLE
            )
            warming_count = sum(
                1 for c in self.containers.values() if c.state == ContainerState.WARMING
            )
            needed = self.config.pool_size - idle_count - warming_count
            if needed <= 0:
                return

            logger.info("Pool needs %d more containers (idle=%d, warming=%d)", needed, idle_count, warming_count)
            tasks = [self._spawn_container() for _ in range(needed)]

        # Spawn outside the lock
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Failed to spawn container: %s", r)

    async def _spawn_container(self) -> ContainerInfo:
        """Create and start a new Lethe container in API mode."""
        port = self._allocate_port()
        container_name = f"lethe-worker-{port}"
        workspace_path = os.path.join(self.config.workspace_base, container_name)
        os.makedirs(workspace_path, exist_ok=True)

        info = ContainerInfo(
            container_id="",
            container_name=container_name,
            state=ContainerState.WARMING,
            port=port,
            workspace_path=workspace_path,
            created_at=time.time(),
        )

        # Build env from the shared env file
        env = {"LETHE_MODE": "api"}
        env_file = self.config.container_env_file
        if os.path.exists(env_file):
            for line in Path(env_file).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()

        try:
            container = self.docker.containers.run(
                self.config.lethe_image,
                detach=True,
                name=container_name,
                labels={self.LABEL: "true"},
                environment=env,
                ports={"8080/tcp": port},
                volumes={
                    workspace_path: {"bind": "/workspace", "mode": "rw"},
                },
                network=self.config.network_name,
                restart_policy={"Name": "unless-stopped"},
            )
            info.container_id = container.id
            self.containers[container.id] = info
            self._save_state()
            logger.info("Spawned container %s on port %d", container_name, port)
        except Exception as e:
            logger.error("Failed to create container %s: %s", container_name, e)
            raise

        # Wait for health check
        asyncio.create_task(self._wait_for_health(info))
        return info

    async def _wait_for_health(self, info: ContainerInfo, timeout: float = 120):
        """Poll /health until container is ready."""
        deadline = time.time() + timeout
        async with httpx.AsyncClient() as client:
            while time.time() < deadline:
                try:
                    r = await client.get(f"{info.api_url}/health", timeout=5)
                    if r.status_code == 200:
                        info.state = ContainerState.IDLE
                        self._save_state()
                        logger.info("Container %s is ready (idle)", info.container_name)
                        return
                except (httpx.ConnectError, httpx.ReadTimeout):
                    pass
                await asyncio.sleep(2)

        logger.error("Container %s failed to become healthy within %ds", info.container_name, timeout)
        # Clean up unhealthy container
        try:
            c = self.docker.containers.get(info.container_id)
            c.remove(force=True)
        except Exception:
            pass
        self.containers.pop(info.container_id, None)
        self._save_state()

    async def get_or_assign(self, user_id: int, metadata: dict) -> Optional[ContainerInfo]:
        """Get the container for a user, or assign one from the pool."""
        # Check existing assignment
        if user_id in self.user_map:
            cid = self.user_map[user_id]
            if cid in self.containers:
                info = self.containers[cid]
                if info.state == ContainerState.ASSIGNED:
                    info.last_activity = time.time()
                    return info
                # Container exists but not assigned — re-check Docker
                try:
                    c = self.docker.containers.get(cid)
                    if c.status == "running":
                        info.state = ContainerState.ASSIGNED
                        info.last_activity = time.time()
                        return info
                except docker.errors.NotFound:
                    pass
            # Stale mapping — need to re-assign
            del self.user_map[user_id]

        # Find workspace from a previous assignment (stopped container)
        existing_workspace = self._find_user_workspace(user_id)

        # Find an idle container
        async with self._lock:
            idle = [c for c in self.containers.values() if c.state == ContainerState.IDLE]
            if not idle:
                logger.warning("No idle containers available! Spawning one on demand...")
                try:
                    new_info = await self._spawn_container()
                    # Wait for it synchronously (not ideal but better than failing)
                    await self._wait_for_health(new_info)
                    if new_info.state != ContainerState.IDLE:
                        return None
                    idle = [new_info]
                except Exception as e:
                    logger.error("On-demand spawn failed: %s", e)
                    return None

            container_info = idle[0]
            container_info.state = ContainerState.ASSIGNED
            container_info.user_id = user_id
            container_info.assigned_at = time.time()
            container_info.last_activity = time.time()
            self.user_map[user_id] = container_info.container_id
            self._save_state()

        # Configure the container with user metadata
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{container_info.api_url}/configure",
                    json={
                        "user_id": user_id,
                        "username": metadata.get("username", ""),
                        "first_name": metadata.get("first_name", ""),
                    },
                    timeout=10,
                )
        except Exception as e:
            logger.warning("Failed to configure container for user %d: %s", user_id, e)

        logger.info(
            "Assigned container %s to user %d (@%s)",
            container_info.container_name,
            user_id,
            metadata.get("username", "?"),
        )

        # Replenish pool in background
        asyncio.create_task(self.ensure_pool())

        return container_info

    def _find_user_workspace(self, user_id: int) -> Optional[str]:
        """Check if we have a preserved workspace for this user."""
        # Look through stopped containers in state
        for info in self.containers.values():
            if info.user_id == user_id and info.state == ContainerState.STOPPED:
                return info.workspace_path
        return None

    def get_container(self, user_id: int) -> Optional[ContainerInfo]:
        """Get container for a user (if assigned)."""
        cid = self.user_map.get(user_id)
        if cid and cid in self.containers:
            return self.containers[cid]
        return None

    async def reap_idle(self):
        """Stop containers that have been idle for too long."""
        cutoff = time.time() - (self.config.idle_timeout_days * 86400)
        to_stop = []
        for info in self.containers.values():
            if info.state == ContainerState.ASSIGNED and info.last_activity:
                if info.last_activity < cutoff:
                    to_stop.append(info)

        for info in to_stop:
            logger.info(
                "Stopping idle container %s (user %d, last activity %ds ago)",
                info.container_name,
                info.user_id or 0,
                int(time.time() - (info.last_activity or 0)),
            )
            try:
                c = self.docker.containers.get(info.container_id)
                c.stop(timeout=10)
                info.state = ContainerState.STOPPED
            except Exception as e:
                logger.warning("Failed to stop container %s: %s", info.container_name, e)

        if to_stop:
            self._save_state()

    def status(self) -> dict:
        """Return pool status summary."""
        states = {}
        for info in self.containers.values():
            states[info.state] = states.get(info.state, 0) + 1
        return {
            "total": len(self.containers),
            "users": len(self.user_map),
            "by_state": states,
        }
