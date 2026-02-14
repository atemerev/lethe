"""NiceGUI-based console UI — Mission Control style."""

import json
import logging
import os
import platform
import psutil
from datetime import datetime, timezone
from typing import Optional

from nicegui import ui, app

from . import get_state

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = 2.0

# Role styling
ROLES = {
    "user":      {"color": "#3b82f6", "bg": "rgba(59,130,246,0.08)", "label": "USER"},
    "assistant": {"color": "#00d4aa", "bg": "rgba(0,212,170,0.08)",  "label": "ASSISTANT"},
    "tool":      {"color": "#f59e0b", "bg": "rgba(245,158,11,0.08)", "label": "TOOL"},
    "system":    {"color": "#64748b", "bg": "rgba(100,116,139,0.1)", "label": "SYSTEM"},
}


def _get_system_info():
    """Get system hardware info (cached on first call)."""
    info = {}
    try:
        info["os"] = f"{platform.system()} {platform.release()}"
        info["cpu"] = platform.processor() or platform.machine()
        info["cores"] = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        info["ram_total"] = f"{mem.total / (1024**3):.0f}GB"
        
        # GPU detection (collapse identical ones)
        gpus = []
        try:
            import subprocess
            from collections import Counter
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                raw = []
                for line in result.stdout.strip().split("\n"):
                    parts = line.split(", ")
                    if len(parts) == 2:
                        raw.append(f"{parts[0].strip()} ({int(parts[1].strip())//1024}GB)")
                counts = Counter(raw)
                for gpu, count in counts.items():
                    gpus.append(f"{count}x{gpu}" if count > 1 else gpu)
        except Exception:
            pass
        info["gpus"] = gpus
    except Exception as e:
        info["error"] = str(e)
    return info


_sys_info = None

def get_sys_info():
    global _sys_info
    if _sys_info is None:
        _sys_info = _get_system_info()
    return _sys_info


CSS = """
<style>
    * { box-sizing: border-box; }
    body { margin: 0; padding: 0; overflow: hidden; background: #0f1419; font-family: 'Inter', -apple-system, sans-serif; }
    
    .mc-root { display: flex; flex-direction: column; width: 100vw; height: 100vh; background: #0f1419; color: #e2e8f0; }
    
    /* Header */
    .mc-header {
        display: flex; flex-direction: column; gap: 0;
        background: #111820; border-bottom: 1px solid #1e2d3d; flex-shrink: 0;
    }
    .mc-header-top {
        display: flex; align-items: center; gap: 12px;
        padding: 6px 16px; border-bottom: 1px solid rgba(0,212,170,0.1);
    }
    .mc-header-bottom {
        display: flex; align-items: center; gap: 16px;
        padding: 4px 16px; background: rgba(0,0,0,0.2);
    }
    .mc-header-chart {
        padding: 2px 16px 3px 16px;
        border-top: 1px solid rgba(30,45,61,0.5);
    }
    .mc-title { font-size: 14px; font-weight: 600; color: #00d4aa; letter-spacing: 2px; text-transform: uppercase; }
    .mc-sep { color: #1e2d3d; }
    .mc-meta { font-size: 10px; color: #475569; font-family: 'JetBrains Mono', monospace; }
    .mc-meta b { color: #94a3b8; font-weight: 500; }
    .mc-meta .accent { color: #00d4aa; }
    .mc-meta .blue { color: #3b82f6; }
    .mc-meta .amber { color: #f59e0b; }
    .mc-meta .green { color: #22c55e; }
    
    /* Status */
    .mc-status { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #94a3b8; font-family: monospace; }
    .mc-dot { width: 8px; height: 8px; border-radius: 50%; }
    .mc-dot-idle { background: #22c55e; }
    .mc-dot-thinking { background: #3b82f6; animation: pulse 1.2s infinite; }
    .mc-dot-tool_call { background: #f59e0b; animation: pulse 0.8s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
    
    /* Columns */
    .mc-columns { display: flex; flex: 1; min-height: 0; overflow: hidden; }
    .mc-panel { display: flex; flex-direction: column; min-width: 0; border-right: 1px solid #1e2d3d; }
    .mc-panel:last-child { border-right: none; }
    .mc-panel-header {
        padding: 8px 12px; font-size: 10px; font-weight: 600; letter-spacing: 1.5px;
        text-transform: uppercase; color: #64748b; background: #111820;
        border-bottom: 1px solid #1e2d3d; flex-shrink: 0;
        display: flex; align-items: center; gap: 8px;
    }
    .mc-panel-header .accent { color: #00d4aa; }
    .mc-panel-content { flex: 1; overflow-y: auto; padding: 8px; }
    .mc-panel-content::-webkit-scrollbar { width: 4px; }
    .mc-panel-content::-webkit-scrollbar-track { background: transparent; }
    .mc-panel-content::-webkit-scrollbar-thumb { background: #2d3f52; border-radius: 2px; }
    .mc-subhead {
        font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 1px;
        margin: 8px 0 4px 0;
    }
    
    /* Message cards */
    .mc-msg {
        border-left: 2px solid; padding: 6px 10px; margin-bottom: 4px;
        border-radius: 2px; font-size: 12px;
        width: 100%;
    }
    .mc-msg-header { display: flex; align-items: center; gap: 6px; margin-bottom: 2px; }
    .mc-msg-role { font-size: 9px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
    .mc-msg-time { font-size: 9px; color: #475569; margin-left: auto; font-family: monospace; }
    .mc-msg-chip { font-size: 9px; padding: 1px 6px; border-radius: 3px; font-weight: 500; }
    .mc-msg pre {
        white-space: pre-wrap; word-wrap: break-word;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 11px; margin: 4px 0 0 0; padding: 0; color: #cbd5e1; line-height: 1.5;
    }
    
    /* Memory blocks */
    .mc-block { margin-bottom: 4px; }
    .mc-block-header {
        display: flex; align-items: center; gap: 6px; padding: 6px 8px;
        cursor: pointer; border-radius: 3px; font-size: 11px; color: #94a3b8;
        transition: background 0.15s;
    }
    .mc-block-header:hover { background: rgba(0,212,170,0.05); }
    .mc-block-arrow { font-size: 10px; color: #475569; transition: transform 0.2s; width: 12px; }
    .mc-block-label { font-weight: 600; color: #e2e8f0; }
    .mc-block-meta { font-size: 9px; color: #475569; margin-left: auto; font-family: monospace; }
    .mc-cache-badge { font-size: 9px; padding: 1px 5px; border-radius: 3px; margin-left: 6px; font-family: monospace; }
    .mc-cache-badge.green { background: rgba(0, 212, 170, 0.15); color: #00d4aa; }
    .mc-cache-badge.amber { background: rgba(255, 191, 0, 0.15); color: #ffbf00; }
    .mc-cache-badge.dim { background: rgba(100, 116, 139, 0.1); color: #64748b; }
    .mc-block-body { padding: 4px 8px 8px 26px; display: none; }
    .mc-block-body.open { display: block; }
    .mc-block-body pre {
        white-space: pre-wrap; word-wrap: break-word;
        font-family: 'JetBrains Mono', monospace;
        font-size: 10px; color: #94a3b8; margin: 0; line-height: 1.5;
    }
    .mc-block-desc { font-size: 10px; color: #475569; margin-bottom: 4px; font-style: italic; }
    
    /* Footer */
    .mc-footer {
        display: flex; align-items: center; gap: 16px;
        padding: 4px 16px; background: #111820;
        border-top: 1px solid #1e2d3d; flex-shrink: 0;
    }
    .mc-footer .mc-meta { font-size: 9px; }
    
    .mc-empty { color: #475569; font-size: 11px; padding: 16px; text-align: center; }
    .mc-badge {
        display: inline-flex; align-items: center; gap: 4px;
        padding: 2px 8px; border-radius: 999px; font-size: 10px;
        border: 1px solid;
    }
    .mc-badge.ok { color: #22c55e; border-color: rgba(34,197,94,0.4); background: rgba(34,197,94,0.08); }
    .mc-badge.warn { color: #f59e0b; border-color: rgba(245,158,11,0.4); background: rgba(245,158,11,0.08); }
    .mc-badge.err { color: #ef4444; border-color: rgba(239,68,68,0.4); background: rgba(239,68,68,0.08); }
    .mc-row {
        display: flex; align-items: center; gap: 6px; font-family: 'JetBrains Mono', monospace;
        font-size: 10px; color: #94a3b8; padding: 2px 0;
    }
    .mc-row b { color: #cbd5e1; font-weight: 500; }
    .mc-chip {
        display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 9px; font-weight: 600;
    }
    .mc-chip.running { color: #22c55e; background: rgba(34,197,94,0.12); }
    .mc-chip.waiting { color: #3b82f6; background: rgba(59,130,246,0.12); }
    .mc-chip.blocked { color: #f59e0b; background: rgba(245,158,11,0.12); }
    .mc-chip.done { color: #64748b; background: rgba(100,116,139,0.15); }
    .mc-chip.error { color: #ef4444; background: rgba(239,68,68,0.12); }

    /* Make context tabs unmissable */
    .mc-tab-shell { display: flex; flex-direction: column; height: 100%; min-height: 0; width: 100%; }
    .mc-tab-panels { flex: 1; min-height: 0; overflow: hidden; width: 100%; }
    .mc-tab-panel { height: 100%; min-height: 0; padding: 0 !important; width: 100%; }
    .mc-tab-scroll { height: 100%; min-height: 0; overflow-y: auto; padding: 8px; width: 100%; }
    .mc-tab-scroll::-webkit-scrollbar { width: 4px; }
    .mc-tab-scroll::-webkit-scrollbar-track { background: transparent; }
    .mc-tab-scroll::-webkit-scrollbar-thumb { background: #2d3f52; border-radius: 2px; }
    .q-tabs { border-bottom: 1px solid #1e2d3d; margin-bottom: 4px; width: 100%; }
    .q-tab { color: #94a3b8; font-size: 11px; min-height: 30px; }
    .q-tab--active { color: #00d4aa !important; font-weight: 700; }
    .q-tab__indicator { background: #00d4aa !important; height: 2px !important; }

    @media (max-width: 1100px) {
        body { overflow: auto; }
        .mc-root { height: auto; min-height: 100vh; }
        .mc-columns { flex-direction: column; overflow: visible; }
        .mc-panel {
            width: 100% !important;
            border-right: none;
            border-bottom: 1px solid #1e2d3d;
            min-height: 280px;
        }
        .mc-header-top, .mc-header-bottom, .mc-footer { flex-wrap: wrap; gap: 8px; }
        .mc-tab-shell { min-height: 320px; }
    }
</style>
<script>
function toggleBlock(id) {
    const el = document.getElementById(id);
    const arrow = document.getElementById('arrow-' + id);
    if (el) {
        el.classList.toggle('open');
        if (arrow) arrow.textContent = el.classList.contains('open') ? '▾' : '▸';
    }
}
</script>
"""


def _esc(text):
    if not isinstance(text, str):
        text = str(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short_model(model):
    """Shorten model name for display."""
    # openrouter/moonshotai/kimi-k2.5 → kimi-k2.5
    parts = model.split("/")
    return parts[-1] if parts else model


def _iso_to_clock(value: str) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return value[:8]


class ConsoleUI:
    
    def __init__(self, port: int = 8080):
        self.port = port
        self._last_version = 0
        self._block_counter = 0
        self._start_time = datetime.now(timezone.utc)
        self._active_context_tab = "LLM"
        self._setup_ui()
    
    def _setup_ui(self):
        @ui.page("/")
        async def main_page():
            ui.dark_mode().enable()
            ui.add_head_html('<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">')
            ui.add_head_html(CSS)
            
            sys_info = get_sys_info()
            state = get_state()
            
            with ui.element("div").classes("mc-root"):
                # ── Header ────────────────────────────────────
                with ui.element("div").classes("mc-header"):
                    # Top row: title + status + model
                    with ui.element("div").classes("mc-header-top"):
                        ui.html('<span class="mc-title">◉ Lethe Console</span>')
                        ui.html('<span class="mc-sep">│</span>')
                        self.status_html = ui.html(self._render_status("idle", None))
                        ui.html('<span class="mc-sep">│</span>')
                        self.alerts_html = ui.html(self._render_health_badges(state))
                        ui.html('<span class="mc-sep">│</span>')
                        model_name = _short_model(state.model) if state.model else "unknown"
                        self.model_html = ui.html(f'<span class="mc-meta">MODEL <b class="accent">{_esc(model_name)}</b></span>')
                        if state.model_aux:
                            aux_name = _short_model(state.model_aux)
                            ui.html(f'<span class="mc-meta">AUX <b>{_esc(aux_name)}</b></span>')
                        ui.html('<span style="flex:1"></span>')
                        self.clock_html = ui.html(self._render_clock())
                    
                    # Bottom row: stats
                    with ui.element("div").classes("mc-header-bottom"):
                        self.stats_html = ui.html(self._render_stats_bar(state))
                    with ui.element("div").classes("mc-header-chart"):
                        self.timeline_html = ui.html(self._render_token_timeline(state))
                
                # ── Columns ───────────────────────────────────
                with ui.element("div").classes("mc-columns"):
                    # Messages — 24%
                    with ui.element("div").classes("mc-panel").style("width: 24%"):
                        ui.html('<div class="mc-panel-header"><span class="accent">◆</span> Messages</div>')
                        self.msg_scroll = ui.element("div").classes("mc-panel-content")
                        with self.msg_scroll:
                            self.msg_container = ui.element("div")
                    
                    # Memory — 18%
                    with ui.element("div").classes("mc-panel").style("width: 18%"):
                        ui.html('<div class="mc-panel-header"><span class="accent">◆</span> Memory</div>')
                        with ui.element("div").classes("mc-panel-content"):
                            self.mem_container = ui.element("div")
                    
                    # Context — 38%
                    with ui.element("div").classes("mc-panel").style("width: 38%"):
                        with ui.element("div").classes("mc-panel-header"):
                            ui.html('<span class="accent">◆</span> Context')
                            ui.html('<span style="flex:1"></span>')
                            self.ctx_info = ui.html('<span class="mc-meta"></span>')
                        with ui.element("div").classes("mc-tab-shell"):
                            with ui.tabs().classes("w-full") as ctx_tabs:
                                tab_llm = ui.tab("LLM")
                                tab_dmn = ui.tab("DMN")
                                tab_hippo = ui.tab("Hippocampus")
                            self.ctx_tabs = ctx_tabs
                            self.ctx_tabs.on("update:model-value", self._on_context_tab_change)
                            with ui.tab_panels(ctx_tabs, value=tab_llm).classes("mc-tab-panels"):
                                with ui.tab_panel(tab_llm).classes("mc-tab-panel"):
                                    self.ctx_scroll = ui.element("div").classes("mc-tab-scroll")
                                    with self.ctx_scroll:
                                        self.ctx_container = ui.element("div").classes("w-full")
                                with ui.tab_panel(tab_dmn).classes("mc-tab-panel"):
                                    self.dmn_ctx_scroll = ui.element("div").classes("mc-tab-scroll")
                                    with self.dmn_ctx_scroll:
                                        self.dmn_ctx_container = ui.element("div").classes("w-full")
                                with ui.tab_panel(tab_hippo).classes("mc-tab-panel"):
                                    self.hippo_ctx_scroll = ui.element("div").classes("mc-tab-scroll")
                                    with self.hippo_ctx_scroll:
                                        self.hippo_ctx_container = ui.element("div").classes("w-full")

                    # Operations — 20%
                    with ui.element("div").classes("mc-panel").style("width: 20%"):
                        ui.html('<div class="mc-panel-header"><span class="accent">◆</span> Ops</div>')
                        self.ops_scroll = ui.element("div").classes("mc-panel-content")
                        with self.ops_scroll:
                            self.ops_container = ui.element("div").classes("w-full")
                
                # ── Footer ────────────────────────────────────
                with ui.element("div").classes("mc-footer"):
                    self.footer_html = ui.html(self._render_footer(state))
                    ui.html('<span class="mc-sep">│</span>')
                    # Static system info
                    hw_parts = []
                    hw_parts.append(f'{sys_info.get("os", "?")}')
                    hw_parts.append(f'{sys_info.get("cpu", "?")}')
                    hw_parts.append(f'{sys_info.get("cores", "?")} cores')
                    hw_parts.append(f'{sys_info.get("ram_total", "?")} RAM')
                    if sys_info.get("gpus"):
                        for gpu in sys_info["gpus"]:
                            hw_parts.append(f'⬡ {gpu}')
                    hw_html = ' <span class="mc-sep">·</span> '.join(
                        f'<b>{_esc(p)}</b>' for p in hw_parts
                    )
                    ui.html(f'<span class="mc-meta">{hw_html}</span>')
                    ui.html('<span style="flex:1"></span>')
                    uptime = self._format_uptime()
                    self.uptime_html = ui.html(f'<span class="mc-meta">UPTIME <b>{uptime}</b></span>')
            
            # Load data
            self._full_rebuild()
            self._last_version = get_state().version
            
            ui.timer(0.5, lambda: (
                self._scroll_bottom(self.msg_scroll),
                self._scroll_bottom(self.ctx_scroll),
            ), once=True)
            
            ui.timer(REFRESH_INTERVAL, self._refresh)
    
    # ── Render helpers ────────────────────────────────────────
    
    def _render_status(self, status, tool):
        dot_cls = f"mc-dot mc-dot-{status}"
        label = status.upper()
        if tool:
            label = f"{status.upper()}: {tool}"
        return f'<span class="mc-status"><span class="{dot_cls}"></span>{_esc(label)}</span>'
    
    def _render_clock(self):
        now = datetime.now()
        return f'<span class="mc-meta" style="font-size:12px"><b>{now.strftime("%H:%M:%S")}</b> <span style="color:#475569">{now.strftime("%b %d")}</span></span>'

    def _render_health_badges(self, state):
        badges = []
        now = datetime.now(timezone.utc)
        dmn = state.dmn or {}
        hip = state.hippocampus or {}

        # DMN health
        dmn_err = dmn.get("last_error", "")
        dmn_last = dmn.get("last_completed_at", "")
        dmn_age_min = 0.0
        if dmn_last:
            try:
                dt = datetime.fromisoformat(dmn_last.replace("Z", "+00:00"))
                dmn_age_min = (now - dt).total_seconds() / 60.0
            except Exception:
                dmn_age_min = 0.0
        if dmn_err:
            badges.append('<span class="mc-badge err">DMN error</span>')
        elif dmn_age_min > 45:
            badges.append(f'<span class="mc-badge warn">DMN stale {int(dmn_age_min)}m</span>')
        else:
            badges.append('<span class="mc-badge ok">DMN ok</span>')

        # Hippocampus health
        h_calls = int(hip.get("calls", 0) or 0)
        h_hit = float(hip.get("hit_rate", 0.0) or 0.0)
        h_fail = int(hip.get("analysis_failures", 0) or 0)
        if h_fail >= 3:
            badges.append('<span class="mc-badge warn">Hippo analysis flaky</span>')
        elif h_calls >= 8 and h_hit < 0.1:
            badges.append('<span class="mc-badge warn">Hippo low hit-rate</span>')
        else:
            badges.append('<span class="mc-badge ok">Hippo ok</span>')

        # Token pressure
        tph = int(state.tokens_per_hour or 0)
        if tph > 250000:
            badges.append(f'<span class="mc-badge err">Token spike {tph:,}/h</span>')
        elif tph > 120000:
            badges.append(f'<span class="mc-badge warn">High tokens {tph:,}/h</span>')
        else:
            badges.append('<span class="mc-badge ok">Token rate normal</span>')

        return '<span class="mc-meta">' + " ".join(badges) + "</span>"

    @staticmethod
    def _token_source_bucket(source: str) -> str:
        src = (source or "").lower()
        if src.startswith("dmn"):
            return "dmn"
        if src.startswith("hippocampus"):
            return "hippocampus"
        if src.startswith("actor:"):
            return "actors"
        if src.startswith("cortex"):
            return "cortex"
        return "other"

    def _render_token_timeline(self, state):
        events = list(state.token_events)
        if not events:
            return '<span class="mc-meta">TOKENS/H timeline: no usage yet</span>'

        now_ts = datetime.now(timezone.utc).timestamp()
        buckets = [{"cortex": 0, "actors": 0, "dmn": 0, "hippocampus": 0, "other": 0} for _ in range(60)]
        for event in events:
            age_minutes = int((now_ts - float(event.get("ts", now_ts))) // 60)
            if age_minutes < 0 or age_minutes >= 60:
                continue
            idx = 59 - age_minutes
            cat = self._token_source_bucket(str(event.get("source", "")))
            buckets[idx][cat] += int(event.get("total", 0) or 0)

        totals = {
            "cortex": sum(b["cortex"] for b in buckets),
            "actors": sum(b["actors"] for b in buckets),
            "dmn": sum(b["dmn"] for b in buckets),
            "hippocampus": sum(b["hippocampus"] for b in buckets),
            "other": sum(b["other"] for b in buckets),
        }
        order = ["cortex", "actors", "dmn", "hippocampus", "other"]
        total_60m = sum(totals.values())
        parts = [f'last 60m <b>{total_60m:,}</b>']
        for key in order:
            if totals[key] > 0:
                parts.append(f'{key}: <b>{totals[key]:,}</b>')
        return (
            '<span class="mc-meta">TOKENS</span> '
            + '<span class="mc-meta">' + ' <span class="mc-sep">│</span> '.join(parts) + '</span>'
        )

    @staticmethod
    def _age_short(iso_ts: str) -> str:
        if not iso_ts:
            return "-"
        try:
            dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            secs = int((datetime.now(timezone.utc) - dt).total_seconds())
            if secs < 60:
                return f"{secs}s"
            if secs < 3600:
                return f"{secs // 60}m"
            return f"{secs // 3600}h"
        except Exception:
            return "-"

    def _render_ops_panel(self, state):
        status = state.actor_system or {}
        actors = status.get("actors", [])
        last_event = status.get("actor_last_event_at", {})
        recent_events = status.get("recent_events", [])
        dmn_history = (state.dmn or {}).get("round_history", [])
        hip_trace = (state.hippocampus or {}).get("recent_trace", [])

        html = []
        html.append('<div class="mc-subhead">Actors</div>')
        html.append(
            f'<div class="mc-row">active <b>{status.get("active_actors", 0)}</b> '
            f'bg tasks <b>{status.get("background_tasks", 0)}</b></div>'
        )
        if actors:
            # Show active first, then recent terminated.
            order = {"running": 0, "initializing": 1, "waiting": 2, "terminated": 3}
            actors_sorted = sorted(actors, key=lambda a: (order.get(a.get("state", ""), 9), a.get("name", "")))
            for a in actors_sorted[:10]:
                a_state = a.get("state", "")
                task_state = a.get("task_state", "")
                last = self._age_short(last_event.get(a.get("id", ""), ""))
                chip_cls = "running" if a_state == "running" else ("waiting" if a_state == "waiting" else ("done" if a_state == "terminated" else "blocked"))
                html.append(
                    f'<div class="mc-row"><span class="mc-chip {chip_cls}">{_esc(a_state)}</span>'
                    f'<b>{_esc(a.get("name", ""))}</b> task:{_esc(task_state)} last:{last}</div>'
                )
        else:
            html.append('<div class="mc-empty">No actor data</div>')

        html.append('<div class="mc-subhead">DMN Rounds</div>')
        if dmn_history:
            for item in list(dmn_history)[-8:][::-1]:
                mode = item.get("mode", "?")
                turns = item.get("turns", 0)
                dur = item.get("duration_seconds", 0)
                forced = " forced" if item.get("forced_deep") else ""
                touched = item.get("touched", "")
                touched_short = touched[:48] + "..." if len(touched) > 48 else touched
                html.append(
                    f'<div class="mc-row"><b>{_esc(mode)}</b> {turns}t {dur}s{forced} '
                    f'{_esc(touched_short or "-")}</div>'
                )
        else:
            html.append('<div class="mc-row">No rounds yet</div>')

        html.append('<div class="mc-subhead">Hippocampus Trace</div>')
        if hip_trace:
            for t in list(hip_trace)[-8:][::-1]:
                d = t.get("decision", "?")
                q = t.get("query", "")
                q_short = (q[:28] + "...") if len(q) > 28 else q
                html.append(
                    f'<div class="mc-row"><b>{_esc(d)}</b> {t.get("latency_ms", 0)}ms '
                    f'{t.get("result_chars", 0)}c {_esc(q_short or "-")}</div>'
                )
        else:
            html.append('<div class="mc-row">No recall trace yet</div>')

        html.append('<div class="mc-subhead">Recent Events</div>')
        if recent_events:
            for e in recent_events[-6:][::-1]:
                html.append(
                    f'<div class="mc-row"><b>{_esc(e.get("type", ""))}</b> '
                    f'{_esc(e.get("actor_id", ""))} {self._age_short(e.get("created_at", ""))} ago</div>'
                )
        else:
            html.append('<div class="mc-row">No recent events</div>')
        return "".join(html)
    
    def _render_stats_bar(self, state):
        parts = []
        parts.append(f'MSGS <b class="blue">{len(state.messages)}</b>')
        parts.append(f'HISTORY <b>{state.total_messages}</b>')
        parts.append(f'ARCHIVAL <b>{state.archival_count}</b>')
        parts.append(
            f'TOKENS <b class="accent">{state.tokens_today:,}</b> '
            f'(in {state.prompt_tokens_today:,} / out {state.completion_tokens_today:,})'
        )
        parts.append(f'TOK/H <b class="green">{int(state.tokens_per_hour):,}</b>')
        parts.append(f'CALLS/H <b>{int(state.api_calls_per_hour)}</b>')
        parts.append(f'API CALLS <b>{state.api_calls_today}</b>')
        if state.last_context_tokens:
            parts.append(f'CTX <b class="amber">{state.last_context_tokens:,}</b> tok')
        if state.tokens_last_total:
            parts.append(
                f'LAST REQ <b>{state.tokens_last_total:,}</b> '
                f'(in {state.tokens_last_prompt:,} / out {state.tokens_last_completion:,})'
            )
        
        # Cache stats
        if state.last_prompt_tokens and state.last_cache_read:
            pct = int(100 * state.last_cache_read / state.last_prompt_tokens) if state.last_prompt_tokens else 0
            parts.append(f'CACHE <b class="green">{pct}%</b> ({state.last_cache_read:,} hit)')
        elif state.last_cache_write:
            parts.append(f'CACHE <b class="amber">{state.last_cache_write:,}</b> write')
        if state.cache_read_tokens:
            parts.append(f'CACHED TODAY <b class="green">{state.cache_read_tokens:,}</b> tok')
        
        dmn = state.dmn or {}
        if dmn:
            dmn_state = dmn.get("state", "idle")
            dmn_mode = dmn.get("last_mode", "-")
            dmn_turns = dmn.get("last_turns", 0)
            parts.append(f'DMN <b>{_esc(dmn_state)}</b> {dmn_mode}/{dmn_turns}t')
        
        hip = state.hippocampus or {}
        if hip:
            hit_rate = int(float(hip.get("hit_rate", 0.0)) * 100)
            parts.append(
                f'HIPPO <b>{hip.get("recalls", 0)}</b>/<b>{hip.get("calls", 0)}</b> '
                f'({hit_rate}% hit)'
            )
        return '<span class="mc-meta">' + ' <span class="mc-sep">│</span> '.join(parts) + '</span>'
    
    def _render_footer(self, state):
        parts = []
        try:
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            parts.append(f'CPU <b>{cpu:.0f}%</b>')
            parts.append(f'MEM <b>{mem.percent:.0f}%</b> ({mem.used/(1024**3):.1f}/{mem.total/(1024**3):.0f}GB)')
        except Exception:
            pass
        
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                for i, line in enumerate(result.stdout.strip().split("\n")):
                    p = [x.strip() for x in line.split(",")]
                    if len(p) == 3:
                        parts.append(f'GPU{i} <b>{p[0]}%</b> VRAM <b>{int(p[1])}/{int(p[2])}MB</b>')
        except Exception:
            pass
        
        if state.last_context_time:
            parts.append(f'LAST CTX <b>{state.last_context_time.strftime("%H:%M:%S")}</b>')
        
        dmn = state.dmn or {}
        if dmn:
            parts.append(
                f'DMN ROUND <b>{int(dmn.get("rounds_total", 0))}</b> '
                f'LAST <b>{_iso_to_clock(dmn.get("last_completed_at", ""))}</b>'
            )
            if dmn.get("last_error"):
                parts.append("DMN ERR <b>yes</b>")
        
        by_source = state.token_totals_by_source or {}
        if by_source:
            top = sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)[:3]
            src_text = ", ".join(f"{k}:{v:,}" for k, v in top)
            parts.append(f'TOP TOKENS <b>{_esc(src_text)}</b>')
        
        return '<span class="mc-meta">' + ' <span class="mc-sep">│</span> '.join(parts) + '</span>' if parts else ''
    
    def _format_uptime(self):
        delta = datetime.now(timezone.utc) - self._start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"
    
    def _render_message_html(self, role, content, timestamp=None):
        r = ROLES.get(role, ROLES["system"])
        time_html = f'<span class="mc-msg-time">{_esc(timestamp)}</span>' if timestamp else ''
        return f'''<div class="mc-msg" style="border-color:{r['color']};background:{r['bg']}">
            <div class="mc-msg-header">
                <span class="mc-msg-role" style="color:{r['color']}">{r['label']}</span>
                {time_html}
            </div>
            <pre>{_esc(content)}</pre>
        </div>'''
    
    def _render_context_msg_html(self, msg):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        r = ROLES.get(role, ROLES["system"])
        
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = "\n---\n".join(parts) if parts else f"[{len(content)} content blocks]"
        
        chips = ""
        tool_detail = ""
        if msg.get("tool_calls"):
            chips += f'<span class="mc-msg-chip" style="background:rgba(245,158,11,0.15);color:#f59e0b">{len(msg["tool_calls"])} tools</span>'
            # Show tool names and args
            lines = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                try:
                    import json
                    args = json.loads(fn.get("arguments", "{}"))
                    args_str = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f'{k}={v}' for k, v in args.items())
                except Exception:
                    args_str = fn.get("arguments", "")[:80]
                lines.append(f"\U0001f527 {name}({args_str})")
            tool_detail = "\n".join(lines)
        if msg.get("tool_call_id"):
            chips += f'<span class="mc-msg-chip" style="background:rgba(245,158,11,0.15);color:#f59e0b">result {_esc(msg["tool_call_id"][:30])}</span>'
        
        # Combine content and tool detail
        display = ""
        if content:
            display += _esc(str(content))
        if tool_detail:
            if display:
                display += "\n"
            display += _esc(tool_detail)
        
        return f'''<div class="mc-msg" style="border-color:{r['color']};background:{r['bg']}">
            <div class="mc-msg-header">
                <span class="mc-msg-role" style="color:{r['color']}">{r['label']}</span>
                {chips}
            </div>
            <pre>{display}</pre>
        </div>'''
    
    # Cache TTL indicators for each block type (Anthropic)
    BLOCK_CACHE_INFO = {
        "identity": ("1h", "green"),    # System prompt — 1h TTL
        "tools": ("1h", "green"),       # Tool reference — 1h TTL
        "summary": ("—", "dim"),        # Summary — NOT cached
    }
    # Default for memory blocks: 5m TTL
    DEFAULT_CACHE_INFO = ("5m", "amber")

    def _render_block_html(self, label, value, description="", chars=0, limit=20000):
        self._block_counter += 1
        bid = f"block-{self._block_counter}"
        desc_html = f'<div class="mc-block-desc">{_esc(description)}</div>' if description else ''
        
        # Cache indicator
        cache_ttl, cache_color = self.BLOCK_CACHE_INFO.get(label, self.DEFAULT_CACHE_INFO)
        cache_badge = f'<span class="mc-cache-badge {cache_color}">⚡{cache_ttl}</span>' if cache_ttl != "—" else '<span class="mc-cache-badge dim">no cache</span>'
        
        return f'''<div class="mc-block">
            <div class="mc-block-header" onclick="toggleBlock('{bid}')">
                <span class="mc-block-arrow" id="arrow-{bid}">▸</span>
                <span class="mc-block-label">{_esc(label)}</span>
                {cache_badge}
                <span class="mc-block-meta">{chars:,}/{limit:,}</span>
            </div>
            <div class="mc-block-body" id="{bid}">
                {desc_html}
                <pre>{_esc(value[:5000])}</pre>
            </div>
        </div>'''

    def _render_text_panel(self, title: str, content: str):
        text = content.strip() if isinstance(content, str) else str(content)
        if not text:
            return f'<div class="mc-empty">No {title} context yet</div>'
        return self._render_message_html("system", text[:12000], None)

    def _estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(text) / 4))

    def _on_context_tab_change(self, e):
        tab = str(getattr(e, "args", "") or "LLM")
        if tab:
            self._active_context_tab = tab
        self._update_context_info(get_state())

    def _update_context_info(self, state):
        tab = self._active_context_tab
        if tab == "DMN":
            text = state.dmn_context or ""
            tok = self._estimate_tokens(text)
            self.ctx_info._props["innerHTML"] = (
                f'<span class="mc-meta">DMN <b class="accent">~{tok:,}</b> tok '
                f'(<b>{len(text):,}</b> chars)</span>'
            )
            self.ctx_info.update()
            return
        if tab == "Hippocampus":
            text = state.hippocampus_context or ""
            tok = self._estimate_tokens(text)
            self.ctx_info._props["innerHTML"] = (
                f'<span class="mc-meta">Hippocampus <b class="accent">~{tok:,}</b> tok '
                f'(<b>{len(text):,}</b> chars)</span>'
            )
            self.ctx_info.update()
            return
        if state.last_context_time:
            time_str = state.last_context_time.strftime("%H:%M:%S")
            self.ctx_info._props["innerHTML"] = (
                f'<span class="mc-meta">LLM <b class="accent">{state.last_context_tokens:,}</b> '
                f'tokens @ {time_str}</span>'
            )
        else:
            self.ctx_info._props["innerHTML"] = '<span class="mc-meta">LLM <b class="accent">0</b> tokens</span>'
        self.ctx_info.update()
    
    # ── Data loading ──────────────────────────────────────────
    
    def _full_rebuild(self):
        state = get_state()
        
        # Messages
        msg_html = []
        for m in state.messages[-30:]:
            msg_html.append(self._render_message_html(
                m.get("role", "?"), m.get("content", ""), m.get("timestamp"),
            ))
        self.msg_container._props["innerHTML"] = "\n".join(msg_html) if msg_html else '<div class="mc-empty">No messages</div>'
        self.msg_container.update()
        
        # Memory
        mem_html = []
        self._block_counter = 0
        if state.identity:
            mem_html.append(self._render_block_html("identity", state.identity, "System prompt", len(state.identity), 20000))
        if state.summary:
            mem_html.append(self._render_block_html("summary", state.summary, "Conversation summary", len(state.summary), 10000))
        for label, block in state.memory_blocks.items():
            if label == "identity":
                continue
            mem_html.append(self._render_block_html(
                label, block.get("value", ""),
                block.get("description", ""),
                len(block.get("value", "")),
                block.get("limit", 20000),
            ))
        self.mem_container._props["innerHTML"] = "\n".join(mem_html) if mem_html else '<div class="mc-empty">No memory blocks</div>'
        self.mem_container.update()
        
        # Context
        ctx_html = []
        if state.last_context:
            for msg in state.last_context:
                ctx_html.append(self._render_context_msg_html(msg))
        self.ctx_container._props["innerHTML"] = "\n".join(ctx_html) if ctx_html else '<div class="mc-empty">No context captured yet</div>'
        self.ctx_container.update()

        self.dmn_ctx_container._props["innerHTML"] = self._render_text_panel("DMN", state.dmn_context)
        self.dmn_ctx_container.update()

        self.hippo_ctx_container._props["innerHTML"] = self._render_text_panel("hippocampus", state.hippocampus_context)
        self.hippo_ctx_container.update()

        # Ops panel
        self.ops_container._props["innerHTML"] = self._render_ops_panel(state)
        self.ops_container.update()
        
        # Context info (depends on active tab)
        self._update_context_info(state)
        
        # Stats bar
        self.stats_html._props["innerHTML"] = self._render_stats_bar(state)
        self.stats_html.update()
        self.alerts_html._props["innerHTML"] = self._render_health_badges(state)
        self.alerts_html.update()
        self.timeline_html._props["innerHTML"] = self._render_token_timeline(state)
        self.timeline_html.update()
        self._update_context_info(state)
    
    def _scroll_bottom(self, el):
        ui.run_javascript(f'document.querySelector("[id=\\"c{el.id}\\"]").scrollTop = 999999;')
    
    # ── Refresh loop ──────────────────────────────────────────
    
    def _refresh(self):
        state = get_state()
        
        # Always update lightweight elements
        self.status_html._props["innerHTML"] = self._render_status(state.status, state.current_tool)
        self.status_html.update()
        
        self.clock_html._props["innerHTML"] = self._render_clock()
        self.clock_html.update()
        
        self.uptime_html._props["innerHTML"] = f'<span class="mc-meta">UPTIME <b>{self._format_uptime()}</b></span>'
        self.uptime_html.update()
        
        self.footer_html._props["innerHTML"] = self._render_footer(state)
        self.footer_html.update()
        
        self.stats_html._props["innerHTML"] = self._render_stats_bar(state)
        self.stats_html.update()
        self.alerts_html._props["innerHTML"] = self._render_health_badges(state)
        self.alerts_html.update()
        self.timeline_html._props["innerHTML"] = self._render_token_timeline(state)
        self.timeline_html.update()
        
        # Rebuild panels only on data change
        if state.version != self._last_version:
            self._last_version = state.version
            self._full_rebuild()
            self._scroll_bottom(self.msg_scroll)
            self._scroll_bottom(self.ctx_scroll)
    
    def run(self):
        logger.info(f"Starting Lethe Console on port {self.port}")
        ui.run(
            port=self.port,
            title="Lethe Console",
            favicon="🧠",
            show=False,
            reload=False,
        )


async def run_console(port: int = 8080):
    console = ConsoleUI(port=port)
    import threading
    threading.Thread(target=console.run, daemon=True).start()
    logger.info(f"Lethe Console started on http://localhost:{port}")
