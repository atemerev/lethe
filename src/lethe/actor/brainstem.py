"""Brainstem - supervisory actor for boot, health, and update orchestration.

Brainstem starts first, performs integrity/resource/update checks, and keeps
the system online. It receives heartbeat ticks on the main heartbeat cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import socket
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import request

from lethe import __version__
from lethe.actor import Actor, ActorConfig, ActorRegistry, ActorState
from lethe.config import Settings
from lethe.memory.openai_oauth import is_oauth_available_openai

logger = logging.getLogger(__name__)

DEFAULT_RELEASE_API = "https://api.github.com/repos/atemerev/lethe/releases/latest"
NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("BRAINSTEM_NOTIFY_COOLDOWN_SECONDS", "21600"))
RESOURCE_WARN_TOKENS_PER_HOUR = int(os.environ.get("BRAINSTEM_TOKENS_PER_HOUR_WARN", "180000"))
RESOURCE_WARN_MEMORY_MB = int(os.environ.get("BRAINSTEM_MEMORY_MB_WARN", "1800"))
ANTHROPIC_WARN_5H_UTIL = float(os.environ.get("BRAINSTEM_ANTHROPIC_5H_UTIL_WARN", "0.85"))
ANTHROPIC_WARN_7D_UTIL = float(os.environ.get("BRAINSTEM_ANTHROPIC_7D_UTIL_WARN", "0.80"))
RUNTIME_STATE_FILE_NAME = "brainstem_runtime_state.json"


def _is_true(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_tag(tag: str) -> str:
    value = (tag or "").strip()
    if value.startswith("v"):
        value = value[1:]
    return value


def _parse_semver(value: str) -> tuple[int, int, int]:
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


class Brainstem:
    """Supervisory bootstrap/health actor."""

    def __init__(
        self,
        registry: ActorRegistry,
        settings: Settings,
        cortex_id: str,
        install_dir: Optional[str] = None,
    ):
        self.registry = registry
        self.settings = settings
        self.cortex_id = cortex_id
        self.install_dir = Path(install_dir or os.getcwd())
        self.release_api = os.environ.get("BRAINSTEM_RELEASE_API", DEFAULT_RELEASE_API).strip() or DEFAULT_RELEASE_API
        self.auto_update_enabled = _is_true("BRAINSTEM_AUTO_UPDATE", "true")
        default_release_checks = "false" if os.environ.get("PYTEST_CURRENT_TEST") else "true"
        self.release_checks_enabled = _is_true("BRAINSTEM_RELEASE_CHECK_ENABLED", default_release_checks)
        self.integrity_checks_enabled = _is_true("BRAINSTEM_INTEGRITY_CHECK_ENABLED", "true")
        self.resource_checks_enabled = _is_true("BRAINSTEM_RESOURCE_CHECK_ENABLED", "true")
        runtime_state_override = os.environ.get("BRAINSTEM_RUNTIME_STATE_FILE", "").strip()
        self._runtime_state_path = Path(runtime_state_override) if runtime_state_override else (
            Path(self.settings.memory_dir) / RUNTIME_STATE_FILE_NAME
        )
        self._session_id = str(uuid.uuid4())[:12]

        self._actor: Optional[Actor] = None
        self._last_heartbeat_at: Optional[datetime] = None
        self._last_notified: dict[str, float] = {}
        self._seen_release_tag = ""
        self._status: dict = {
            "state": "idle",
            "started_at": "",
            "last_heartbeat_at": "",
            "heartbeat_count": 0,
            "auto_update_enabled": self.auto_update_enabled,
            "release_checks_enabled": self.release_checks_enabled,
            "current_version": "",
            "latest_release": "",
            "update_available": False,
            "last_update_attempt_at": "",
            "last_update_result": "",
            "last_resource_snapshot": {},
            "last_integrity": {"ok": True, "issues": [], "warnings": []},
            "last_restart_detected": {},
            "last_shutdown_at": "",
            "last_error": "",
        }
        self._history: deque[dict] = deque(maxlen=40)

    @property
    def actor(self) -> Optional[Actor]:
        return self._actor

    @property
    def status(self) -> dict:
        data = dict(self._status)
        data["history"] = list(self._history)
        return data

    async def startup(self):
        """Spawn and run initial startup checks."""
        if self._actor is None or self._actor.state == ActorState.TERMINATED:
            self._actor = self.registry.spawn(
                ActorConfig(
                    name="brainstem",
                    group="main",
                    goals=(
                        "Boot and supervise Lethe runtime. Check integrity/resources/releases. "
                        "Notify cortex only for meaningful findings."
                    ),
                ),
                spawned_by=self.cortex_id,
            )
        self._status["current_version"] = self._detect_current_version()
        self._status["state"] = "booting"
        self._status["started_at"] = _now_iso()
        restart_info = self._detect_restart()
        self._touch_runtime_state(started=True)
        if restart_info:
            self._status["last_restart_detected"] = restart_info
            summary = self._format_restart_summary(restart_info)
            await self._send_task_update(
                f"Brainstem startup: restart detected ({summary})",
                kind="restart_detected",
            )
            await self._send_user_notify(
                (
                    "Lethe restarted. "
                    f"{summary}. "
                    "If useful, I can review what was in-flight before restart."
                ),
                kind="brainstem_restart",
            )
        await self._run_cycle(trigger="startup", heartbeat_message="")
        self._status["state"] = "online"

    async def heartbeat(self, heartbeat_message: str = ""):
        """Run supervisory checks on heartbeat ticks."""
        now = datetime.now(timezone.utc)
        if self._last_heartbeat_at and (now - self._last_heartbeat_at).total_seconds() < 30:
            return
        self._last_heartbeat_at = now
        self._status["last_heartbeat_at"] = now.isoformat()
        self._status["heartbeat_count"] = int(self._status.get("heartbeat_count", 0)) + 1
        self._touch_runtime_state()
        if self._status.get("state") == "idle":
            self._status["state"] = "online"
        await self._run_cycle(trigger="heartbeat", heartbeat_message=heartbeat_message or "")

    def record_shutdown(self):
        """Persist a clean-shutdown marker for restart diagnostics."""
        self._status["last_shutdown_at"] = _now_iso()
        self._touch_runtime_state(shutdown=True)

    async def _run_cycle(self, trigger: str, heartbeat_message: str):
        if not self._actor or self._actor.state == ActorState.TERMINATED:
            return

        findings: list[str] = []
        notify_items: list[tuple[str, str]] = []
        self._status["last_error"] = ""

        try:
            current_version = self._detect_current_version()
            self._status["current_version"] = current_version

            release_info = await self._check_release(current_version)
            self._status["latest_release"] = release_info.get("latest_tag", "")
            self._status["update_available"] = bool(release_info.get("update_available"))

            if release_info.get("error"):
                findings.append(f"release-check error: {release_info['error']}")
            elif release_info.get("update_available"):
                latest = release_info.get("latest_tag", "")
                findings.append(f"new release available: {latest} (current: {current_version})")
                notify_items.append(
                    (
                        f"release:{latest}",
                        f"Brainstem detected Lethe {latest} (current {current_version}). "
                        f"Update available.",
                    )
                )
                await self._maybe_auto_update(latest_tag=latest)

            if self.resource_checks_enabled:
                resources = self._collect_resource_snapshot()
                self._status["last_resource_snapshot"] = resources
                if resources.get("tokens_per_hour", 0) >= RESOURCE_WARN_TOKENS_PER_HOUR:
                    findings.append(
                        f"high token rate: {int(resources.get('tokens_per_hour', 0))}/h"
                    )
                    notify_items.append(
                        (
                            "resource:tokens",
                            "Brainstem warning: token usage rate is high. "
                            "Consider throttling background activity.",
                        )
                    )
                if resources.get("process_rss_mb", 0) >= RESOURCE_WARN_MEMORY_MB:
                    findings.append(
                        f"high memory: {int(resources.get('process_rss_mb', 0))} MB RSS"
                    )
                    notify_items.append(
                        (
                            "resource:memory",
                            "Brainstem warning: process memory is high.",
                        )
                    )
                ratelimit = resources.get("anthropic_ratelimit") or {}
                if ratelimit:
                    unified_status = str(ratelimit.get("unified_status", "")).lower()
                    five = ratelimit.get("five_hour", {}) or {}
                    seven = ratelimit.get("seven_day", {}) or {}
                    five_util = five.get("utilization")
                    seven_util = seven.get("utilization")
                    if unified_status and unified_status != "allowed":
                        findings.append(f"anthropic unified status: {unified_status}")
                        notify_items.append(
                            (
                                "resource:anthropic_status",
                                f"Brainstem warning: Anthropic unified ratelimit status is '{unified_status}'.",
                            )
                        )
                    near_limit = (
                        (isinstance(five_util, (float, int)) and five_util >= ANTHROPIC_WARN_5H_UTIL)
                        or (isinstance(seven_util, (float, int)) and seven_util >= ANTHROPIC_WARN_7D_UTIL)
                    )
                    if near_limit:
                        five_pct = f"{float(five_util) * 100:.0f}%" if isinstance(five_util, (float, int)) else "n/a"
                        seven_pct = f"{float(seven_util) * 100:.0f}%" if isinstance(seven_util, (float, int)) else "n/a"
                        findings.append(f"anthropic ratelimit near cap (5h={five_pct}, 7d={seven_pct})")
                        notify_items.append(
                            (
                                "resource:anthropic_near",
                                "Brainstem warning: Anthropic ratelimit utilization is near cap "
                                f"(5h={five_pct}, 7d={seven_pct}).",
                            )
                        )

            if self.integrity_checks_enabled:
                integrity = self._check_integrity(current_version=current_version)
                self._status["last_integrity"] = integrity
                if not integrity.get("ok", True):
                    issues = integrity.get("issues", [])
                    findings.append(f"integrity issues: {len(issues)}")
                    notify_items.append(
                        (
                            "integrity:issues",
                            "Brainstem detected integrity issues. "
                            "Please check `/status` and logs.",
                        )
                    )
                elif integrity.get("warnings"):
                    findings.append(f"integrity warnings: {len(integrity['warnings'])}")

            if heartbeat_message:
                findings.append("heartbeat received")

            summary = "; ".join(findings) if findings else "all checks healthy"
            await self._send_task_update(
                f"Brainstem {trigger}: {summary}",
                kind=f"brainstem_{trigger}",
            )
            for key, message in notify_items:
                if self._should_notify(key):
                    await self._send_user_notify(message, kind="brainstem_alert")

            self._history.append(
                {
                    "at": _now_iso(),
                    "trigger": trigger,
                    "findings": findings,
                    "summary": summary,
                }
            )
        except Exception as e:
            self._status["last_error"] = str(e)
            logger.warning("Brainstem cycle failed: %s", e, exc_info=True)
            await self._send_task_update(f"Brainstem {trigger} error: {e}", kind="brainstem_error")

    @staticmethod
    def _parse_iso(value: str) -> Optional[datetime]:
        text = (value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _load_runtime_state(self) -> dict:
        path = self._runtime_state_path
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _touch_runtime_state(self, started: bool = False, shutdown: bool = False):
        now = datetime.now(timezone.utc)
        path = self._runtime_state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            state = self._load_runtime_state()
            if started:
                state = {}
                state["session_id"] = self._session_id
                state["started_at"] = self._status.get("started_at") or now.isoformat()
                state["pid"] = os.getpid()
                state["host"] = socket.gethostname()
                state["version"] = self._status.get("current_version") or self._detect_current_version()
                state["clean_shutdown"] = False
                state["shutdown_at"] = ""
            state["last_seen_at"] = now.isoformat()
            if shutdown:
                state["clean_shutdown"] = True
                state["shutdown_at"] = now.isoformat()
            path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")
        except Exception as e:
            logger.debug("Brainstem runtime state update failed: %s", e)

    def _detect_restart(self) -> dict:
        data = self._load_runtime_state()
        if not data:
            return {}
        session_id = str(data.get("session_id", "")).strip()
        if not session_id:
            return {}
        now = datetime.now(timezone.utc)
        previous_started = self._parse_iso(str(data.get("started_at", "")))
        previous_seen = self._parse_iso(str(data.get("last_seen_at", "")))
        previous_shutdown = self._parse_iso(str(data.get("shutdown_at", "")))
        anchor = previous_seen or previous_started
        downtime_seconds = 0
        if anchor:
            downtime_seconds = max(0, int((now - anchor).total_seconds()))
        return {
            "session_id": session_id,
            "previous_started_at": previous_started.isoformat() if previous_started else "",
            "previous_last_seen_at": previous_seen.isoformat() if previous_seen else "",
            "previous_shutdown_at": previous_shutdown.isoformat() if previous_shutdown else "",
            "previous_clean_shutdown": bool(data.get("clean_shutdown", False)),
            "previous_pid": int(data.get("pid", 0) or 0),
            "previous_version": str(data.get("version", "")),
            "downtime_seconds": downtime_seconds,
        }

    @staticmethod
    def _format_restart_summary(info: dict) -> str:
        downtime_seconds = int(info.get("downtime_seconds", 0) or 0)
        if downtime_seconds < 120:
            downtime_str = f"downtime about {downtime_seconds}s"
        elif downtime_seconds < 3600:
            downtime_str = f"downtime about {downtime_seconds // 60}m"
        else:
            downtime_str = f"downtime about {downtime_seconds // 3600}h"
        previous_clean = bool(info.get("previous_clean_shutdown", False))
        shutdown_str = "previous shutdown looked clean" if previous_clean else "previous shutdown may have been abrupt"
        prev_version = str(info.get("previous_version", "")).strip()
        version_str = f"last version was {prev_version}" if prev_version else "previous version unknown"
        return f"{downtime_str}; {shutdown_str}; {version_str}"

    async def _send_task_update(self, text: str, kind: str = "brainstem"):
        if not self._actor:
            return
        try:
            await self._actor.send_to(
                self.cortex_id,
                text,
                metadata={"channel": "task_update", "kind": kind},
            )
        except Exception as e:
            logger.debug("Brainstem task update failed: %s", e)

    async def _send_user_notify(self, text: str, kind: str = "brainstem_alert"):
        if not self._actor:
            return
        try:
            await self._actor.send_to(
                self.cortex_id,
                text,
                metadata={"channel": "user_notify", "kind": kind},
            )
        except Exception as e:
            logger.debug("Brainstem user notify failed: %s", e)

    def _should_notify(self, key: str) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_notified.get(key, 0.0)
        if now - last < NOTIFY_COOLDOWN_SECONDS:
            return False
        self._last_notified[key] = now
        return True

    def _detect_current_version(self) -> str:
        pyproject = self.install_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
                if m:
                    return m.group(1).strip()
            except Exception:
                pass
        return _normalize_tag(__version__)

    async def _check_release(self, current_version: str) -> dict:
        if not self.release_checks_enabled:
            return {"latest_tag": "", "update_available": False}
        try:
            latest = await asyncio.to_thread(self._fetch_latest_release_tag)
            latest_norm = _normalize_tag(latest)
            current_norm = _normalize_tag(current_version)
            update_available = _parse_semver(latest_norm) > _parse_semver(current_norm)
            return {
                "latest_tag": latest,
                "update_available": update_available,
            }
        except Exception as e:
            return {"latest_tag": "", "update_available": False, "error": str(e)}

    def _fetch_latest_release_tag(self) -> str:
        req = request.Request(
            self.release_api,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "lethe-brainstem"},
            method="GET",
        )
        with request.urlopen(req, timeout=3.0) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
        data = json.loads(payload)
        tag = str(data.get("tag_name", "")).strip()
        if not tag:
            raise RuntimeError("release API response missing tag_name")
        return tag

    async def _maybe_auto_update(self, latest_tag: str):
        if not latest_tag:
            return
        if self._seen_release_tag == latest_tag:
            return
        self._seen_release_tag = latest_tag

        if not self.auto_update_enabled:
            await self._send_task_update(
                f"Brainstem: update {latest_tag} detected but auto-update is disabled.",
                kind="update_skipped",
            )
            return

        script = self.install_dir / "update.sh"
        if not script.exists():
            await self._send_task_update(
                f"Brainstem: update {latest_tag} detected, but update.sh is missing.",
                kind="update_skipped",
            )
            return

        if self._repo_dirty():
            await self._send_task_update(
                (
                    f"Brainstem: update {latest_tag} detected with local changes present; "
                    "proceeding via update.sh with safety backup (git stash)."
                ),
                kind="update_backup",
            )

        self._status["last_update_attempt_at"] = _now_iso()
        await self._send_task_update(
            f"Brainstem: applying update {latest_tag} via update.sh.",
            kind="update_start",
        )
        ok, output = await self._run_update_script(script)
        result = "success" if ok else "failed"
        self._status["last_update_result"] = f"{result}: {output[:240]}"
        await self._send_task_update(
            f"Brainstem: update {latest_tag} {result}. {output}",
            kind="update_result",
        )
        if ok:
            await self._send_user_notify(
                (
                    f"Brainstem updated Lethe to {latest_tag}. "
                    "New version is available now. "
                    "If your runtime did not auto-restart, please restart Lethe to apply it."
                ),
                kind="brainstem_update_ready",
            )

    async def _run_update_script(self, script_path: Path) -> tuple[bool, str]:
        cmd = os.environ.get("BRAINSTEM_UPDATE_COMMAND", f"bash {shlex.quote(str(script_path))}")
        parts = shlex.split(cmd)
        if not parts:
            return False, "empty update command"
        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                cwd=str(self.install_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
            output = (out or b"").decode("utf-8", errors="replace").strip()
            err_text = (err or b"").decode("utf-8", errors="replace").strip()
            merged = output or err_text or f"exit={proc.returncode}"
            return proc.returncode == 0, merged
        except asyncio.TimeoutError:
            return False, "update command timed out"
        except Exception as e:
            return False, f"update command failed: {e}"

    def _repo_dirty(self) -> bool:
        git_dir = self.install_dir / ".git"
        if not git_dir.exists():
            return False
        try:
            import subprocess

            result = subprocess.run(
                ["git", "-C", str(self.install_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            return bool((result.stdout or "").strip())
        except Exception:
            return False

    def _collect_resource_snapshot(self) -> dict:
        data = {
            "tokens_today": 0,
            "tokens_per_hour": 0,
            "api_calls_per_hour": 0,
            "process_rss_mb": 0,
            "workspace_free_gb": 0.0,
            "auth_mode": "unknown",
            "anthropic_ratelimit": {},
        }
        try:
            from lethe.console import get_state

            state = get_state()
            data["tokens_today"] = int(getattr(state, "tokens_today", 0) or 0)
            data["tokens_per_hour"] = int(getattr(state, "tokens_per_hour", 0) or 0)
            data["api_calls_per_hour"] = int(getattr(state, "api_calls_per_hour", 0) or 0)
            data["anthropic_ratelimit"] = dict(getattr(state, "anthropic_ratelimit", {}) or {})
        except Exception:
            pass

        try:
            import resource

            rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
            if rss_kb > 0:
                # Linux ru_maxrss is kilobytes.
                data["process_rss_mb"] = int(rss_kb / 1024)
        except Exception:
            pass

        try:
            usage = shutil.disk_usage(str(self.settings.workspace_dir))
            data["workspace_free_gb"] = round(usage.free / (1024 ** 3), 2)
        except Exception:
            pass

        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            data["auth_mode"] = "subscription_oauth"
        elif is_oauth_available_openai():
            data["auth_mode"] = "openai_subscription_oauth"
        elif os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
            data["auth_mode"] = "api_key_credits"
        return data

    def _check_integrity(self, current_version: str) -> dict:
        issues: list[str] = []
        warnings: list[str] = []
        required_paths = [
            ("workspace_dir", Path(self.settings.workspace_dir)),
            ("memory_dir", Path(self.settings.memory_dir)),
            ("config_dir", Path(self.settings.lethe_config_dir)),
            ("db_parent", Path(self.settings.db_path).parent),
        ]
        for label, path in required_paths:
            if not path.exists():
                issues.append(f"{label} missing: {path}")

        principal = self.registry.get(self.cortex_id)
        if not principal or principal.state == ActorState.TERMINATED:
            issues.append("cortex principal actor missing or terminated")

        pyproject = self.install_dir / "pyproject.toml"
        if not pyproject.exists():
            warnings.append(f"pyproject.toml not found at {pyproject}")
        else:
            detected = self._detect_current_version()
            if _normalize_tag(detected) != _normalize_tag(current_version):
                warnings.append(
                    f"version mismatch (detected={detected}, running={current_version})"
                )

        if self.install_dir.joinpath(".git").exists() and self._repo_dirty():
            warnings.append("repository has local uncommitted changes")

        return {"ok": not issues, "issues": issues, "warnings": warnings}

    def get_context_view(self, max_history: int = 10) -> str:
        """Build a dashboard-friendly Brainstem context snapshot."""
        status = self.status
        resources = status.get("last_resource_snapshot", {}) or {}
        integrity = status.get("last_integrity", {}) or {}
        history = list(status.get("history", []))[-max_history:]

        lines = [
            "# Brainstem Context",
            "",
            f"- state: {status.get('state', 'idle')}",
            f"- started_at: {status.get('started_at') or '-'}",
            f"- last_heartbeat_at: {status.get('last_heartbeat_at') or '-'}",
            f"- heartbeat_count: {int(status.get('heartbeat_count', 0) or 0)}",
            f"- current_version: {status.get('current_version') or '-'}",
            f"- latest_release: {status.get('latest_release') or '-'}",
            f"- update_available: {bool(status.get('update_available', False))}",
            f"- auto_update_enabled: {bool(status.get('auto_update_enabled', False))}",
            f"- release_checks_enabled: {bool(status.get('release_checks_enabled', False))}",
            f"- last_update_attempt_at: {status.get('last_update_attempt_at') or '-'}",
            f"- last_update_result: {status.get('last_update_result') or '-'}",
            f"- last_shutdown_at: {status.get('last_shutdown_at') or '-'}",
            f"- last_restart_detected: {bool(status.get('last_restart_detected'))}",
            f"- last_error: {status.get('last_error') or '-'}",
            "",
            "## Resources",
            f"- tokens_today: {int(resources.get('tokens_today', 0) or 0)}",
            f"- tokens_per_hour: {int(resources.get('tokens_per_hour', 0) or 0)}",
            f"- api_calls_per_hour: {int(resources.get('api_calls_per_hour', 0) or 0)}",
            f"- process_rss_mb: {int(resources.get('process_rss_mb', 0) or 0)}",
            f"- workspace_free_gb: {resources.get('workspace_free_gb', 0.0)}",
            f"- auth_mode: {resources.get('auth_mode', 'unknown')}",
            "",
            "## Integrity",
            f"- ok: {bool(integrity.get('ok', True))}",
            f"- issues: {len(integrity.get('issues', []) or [])}",
            f"- warnings: {len(integrity.get('warnings', []) or [])}",
            "",
            "## Recent Cycles",
        ]
        if not history:
            lines.append("- (no cycles yet)")
        else:
            for item in history[::-1]:
                at = item.get("at", "-")
                trigger = item.get("trigger", "-")
                summary = str(item.get("summary", "")).strip()
                if len(summary) > 240:
                    summary = summary[:240] + "..."
                lines.append(f"- {at} [{trigger}] {summary or '(no summary)'}")
        return "\n".join(lines)
