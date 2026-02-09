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
    .mc-title { font-size: 14px; font-weight: 600; color: #00d4aa; letter-spacing: 2px; text-transform: uppercase; }
    .mc-sep { color: #1e2d3d; }
    .mc-meta { font-size: 10px; color: #475569; font-family: 'JetBrains Mono', monospace; }
    .mc-meta b { color: #94a3b8; font-weight: 500; }
    .mc-meta .accent { color: #00d4aa; }
    .mc-meta .blue { color: #3b82f6; }
    .mc-meta .amber { color: #f59e0b; }
    
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
    
    /* Message cards */
    .mc-msg {
        border-left: 2px solid; padding: 6px 10px; margin-bottom: 4px;
        border-radius: 2px; font-size: 12px;
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


class ConsoleUI:
    
    def __init__(self, port: int = 8080):
        self.port = port
        self._last_version = 0
        self._block_counter = 0
        self._start_time = datetime.now(timezone.utc)
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
                
                # ── Columns ───────────────────────────────────
                with ui.element("div").classes("mc-columns"):
                    # Messages — 30%
                    with ui.element("div").classes("mc-panel").style("width: 30%"):
                        ui.html('<div class="mc-panel-header"><span class="accent">◆</span> Messages</div>')
                        self.msg_scroll = ui.element("div").classes("mc-panel-content")
                        with self.msg_scroll:
                            self.msg_container = ui.element("div")
                    
                    # Memory — 20%
                    with ui.element("div").classes("mc-panel").style("width: 20%"):
                        ui.html('<div class="mc-panel-header"><span class="accent">◆</span> Memory</div>')
                        with ui.element("div").classes("mc-panel-content"):
                            self.mem_container = ui.element("div")
                    
                    # Context — 50%
                    with ui.element("div").classes("mc-panel").style("width: 50%"):
                        with ui.element("div").classes("mc-panel-header"):
                            ui.html('<span class="accent">◆</span> Context')
                            ui.html('<span style="flex:1"></span>')
                            self.ctx_info = ui.html('<span class="mc-meta"></span>')
                        self.ctx_scroll = ui.element("div").classes("mc-panel-content")
                        with self.ctx_scroll:
                            self.ctx_container = ui.element("div")
                
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
    
    def _render_stats_bar(self, state):
        parts = []
        parts.append(f'MSGS <b class="blue">{len(state.messages)}</b>')
        parts.append(f'HISTORY <b>{state.total_messages}</b>')
        parts.append(f'ARCHIVAL <b>{state.archival_count}</b>')
        parts.append(f'TOKENS TODAY <b class="accent">{state.tokens_today:,}</b>')
        parts.append(f'API CALLS <b>{state.api_calls_today}</b>')
        if state.last_context_tokens:
            parts.append(f'CTX <b class="amber">{state.last_context_tokens:,}</b> tok')
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
    
    def _render_block_html(self, label, value, description="", chars=0, limit=20000):
        self._block_counter += 1
        bid = f"block-{self._block_counter}"
        desc_html = f'<div class="mc-block-desc">{_esc(description)}</div>' if description else ''
        return f'''<div class="mc-block">
            <div class="mc-block-header" onclick="toggleBlock('{bid}')">
                <span class="mc-block-arrow" id="arrow-{bid}">▸</span>
                <span class="mc-block-label">{_esc(label)}</span>
                <span class="mc-block-meta">{chars:,}/{limit:,}</span>
            </div>
            <div class="mc-block-body" id="{bid}">
                {desc_html}
                <pre>{_esc(value[:5000])}</pre>
            </div>
        </div>'''
    
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
        
        # Context info
        if state.last_context_time:
            time_str = state.last_context_time.strftime("%H:%M:%S")
            self.ctx_info._props["innerHTML"] = f'<span class="mc-meta"><b class="accent">{state.last_context_tokens:,}</b> tokens @ {time_str}</span>'
            self.ctx_info.update()
        
        # Stats bar
        self.stats_html._props["innerHTML"] = self._render_stats_bar(state)
        self.stats_html.update()
    
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
