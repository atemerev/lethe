"""NiceGUI-based console UI."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from nicegui import ui, app

from . import get_state, ConsoleState

logger = logging.getLogger(__name__)

# Refresh interval in seconds
REFRESH_INTERVAL = 2.0

# Role colors and styling (light theme)
ROLE_STYLES = {
    "user": {"bg": "bg-blue-50", "border": "border-blue-400", "icon": "person"},
    "assistant": {"bg": "bg-emerald-50", "border": "border-emerald-400", "icon": "smart_toy"},
    "tool": {"bg": "bg-amber-50", "border": "border-amber-400", "icon": "build"},
    "system": {"bg": "bg-slate-100", "border": "border-slate-400", "icon": "settings"},
}


class ConsoleUI:
    """Mind state visualization console."""
    
    def __init__(self, port: int = 8080):
        self.port = port
        self._last_context_time = None
        self._last_message_count = 0
        self._last_version = 0
        self._setup_ui()
    
    def _setup_ui(self):
        """Set up the UI layout."""
        
        @ui.page("/")
        async def main_page():
            state = get_state()
            
            # Light theme
            ui.dark_mode().disable()
            
            # Custom CSS
            ui.add_head_html('''
            <style>
                .message-block, .context-block { 
                    border-left: 3px solid; 
                    padding: 8px 12px; 
                    margin: 4px 0;
                    border-radius: 4px;
                }
                .content-full {
                    white-space: pre-wrap;
                    word-wrap: break-word;
                    font-family: monospace;
                    font-size: 11px;
                    margin: 8px 0 0 0;
                    padding: 0;
                }
            </style>
            ''')
            
            # Header - compact
            with ui.header().classes("bg-slate-100 border-b border-gray-200 py-1 min-h-0"):
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.label("🧠 Lethe Console").classes("text-subtitle1 font-medium text-slate-700")
                    ui.space()
                    self.status_chip = ui.chip("idle", icon="circle", color="light-green").props("dense")
                    self.stats_label = ui.label("").classes("text-sm text-slate-600")
            
            # Main layout - 3 columns (flex, no wrap)
            with ui.element("div").classes("flex flex-nowrap w-full h-screen bg-white"):
                # Messages column - 30%
                with ui.element("div").classes("w-[30%] min-w-0 h-full border-r border-gray-200 flex-shrink-0 flex flex-col"):
                    ui.label("💬 Messages").classes("text-subtitle1 font-medium px-3 py-2 bg-slate-200 border-b border-gray-300 flex-shrink-0")
                    self.messages_scroll = ui.element("div").classes("flex-1 overflow-y-auto")
                    with self.messages_scroll:
                        self.messages_container = ui.column().classes("w-full p-2 gap-1")
                
                # Memory column - 20%
                with ui.element("div").classes("w-[20%] min-w-0 h-full border-r border-gray-200 flex-shrink-0 flex flex-col"):
                    ui.label("🧠 Memory").classes("text-subtitle1 font-medium px-3 py-2 bg-slate-200 border-b border-gray-300 flex-shrink-0")
                    with ui.element("div").classes("flex-1 overflow-y-auto"):
                        self.blocks_container = ui.column().classes("w-full p-2")
                
                # Context column - 50%
                with ui.element("div").classes("w-[50%] min-w-0 h-full flex-shrink-0 flex flex-col"):
                    with ui.row().classes("w-full items-center px-3 py-2 bg-slate-200 border-b border-gray-300 flex-shrink-0"):
                        ui.label("📤 Context").classes("text-subtitle1 font-medium")
                        self.context_info = ui.chip("", icon="token").props("dense").classes("ml-4")
                    self.context_scroll = ui.element("div").classes("flex-1 overflow-y-auto")
                    with self.context_scroll:
                        self.context_container = ui.column().classes("w-full p-2 gap-1")
            
            # Initial data load
            self._load_initial_data()
            self._last_version = get_state().version
            
            # Scroll to bottom after initial render
            ui.timer(0.5, lambda: (
                self._scroll_to_bottom(self.messages_scroll),
                self._scroll_to_bottom(self.context_scroll),
            ), once=True)
            
            # Start refresh timer
            ui.timer(REFRESH_INTERVAL, self._refresh_ui)
    
    def _render_message(self, container, role: str, content: str, timestamp: str = None):
        """Render a single message block."""
        style = ROLE_STYLES.get(role, ROLE_STYLES["system"])
        
        display_content = content if isinstance(content, str) else str(content)
        
        with container:
            with ui.card().classes(f"w-full {style['bg']} message-block {style['border']}"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(style["icon"]).classes("text-lg")
                    ui.label(role.upper()).classes("font-bold text-sm")
                    if timestamp:
                        ui.label(timestamp).classes("text-xs text-gray-400 ml-auto")
                ui.html(f"<pre class='content-full'>{display_content}</pre>")
    
    def _render_context_message(self, container, msg: dict):
        """Render a context message block."""
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        style = ROLE_STYLES.get(role, ROLE_STYLES["system"])
        
        # Handle different content types
        if isinstance(content, list):
            # System message with content blocks - extract text
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content_display = "\n---\n".join(parts) if parts else f"[{len(content)} content blocks]"
        elif isinstance(content, str):
            content_display = content
        else:
            content_display = str(content)
        
        with container:
            with ui.card().classes(f"w-full {style['bg']} context-block {style['border']}"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(style["icon"]).classes("text-lg")
                    ui.label(role.upper()).classes("font-bold text-sm")
                    if msg.get("tool_calls"):
                        ui.chip(f"{len(msg['tool_calls'])} tools", icon="build", color="yellow").classes("ml-2")
                    if msg.get("tool_call_id"):
                        ui.chip("result", icon="check", color="orange").classes("ml-2")
                ui.html(f"<pre class='content-full'>{content_display}</pre>")
    
    def _load_initial_data(self):
        """Load initial data into UI."""
        state = get_state()
        
        # Render messages (chronological - oldest first, newest at bottom)
        self.messages_container.clear()
        for msg in state.messages[-30:]:
            self._render_message(
                self.messages_container,
                msg.get("role", "?"),
                msg.get("content", ""),
                msg.get("timestamp")
            )
        
        # Build memory blocks
        self._rebuild_blocks()
        
        # Build context view
        self._rebuild_context()
    
    def _rebuild_blocks(self):
        """Rebuild memory blocks display."""
        state = get_state()
        self.blocks_container.clear()
        
        with self.blocks_container:
            # Identity
            if state.identity:
                with ui.expansion("🎭 Identity (System Prompt)", icon="person").classes("w-full"):
                    ui.html(f"<pre style='white-space:pre-wrap;font-size:11px;max-height:400px;overflow:auto'>{state.identity[:3000]}</pre>")
            
            # Summary
            if state.summary:
                with ui.expansion("📝 Conversation Summary", icon="summarize").classes("w-full"):
                    ui.html(f"<pre style='white-space:pre-wrap;font-size:11px;max-height:300px;overflow:auto'>{state.summary[:2000]}</pre>")
            
            # Memory blocks
            if state.memory_blocks:
                ui.label("Memory Blocks").classes("text-h6 mt-4")
                
                for label, block in state.memory_blocks.items():
                    if label == "identity":
                        continue
                    
                    value = block.get("value", "")
                    chars = len(value)
                    limit = block.get("limit", 20000)
                    description = block.get("description", "")
                    
                    # Icon based on block name
                    icon = "memory"
                    if "persona" in label or "capabil" in label:
                        icon = "psychology"
                    elif "human" in label:
                        icon = "person"
                    elif "project" in label:
                        icon = "folder"
                    elif "task" in label:
                        icon = "checklist"
                    elif "tool" in label:
                        icon = "build"
                    
                    with ui.expansion(f"{label} ({chars:,}/{limit:,} chars)", icon=icon).classes("w-full"):
                        if description:
                            ui.label(description).classes("text-caption text-gray-400 mb-2")
                        ui.html(f"<pre style='white-space:pre-wrap;font-size:11px;max-height:300px;overflow:auto'>{value[:4000]}</pre>")
            
            if not state.memory_blocks and not state.identity:
                ui.label("No memory blocks loaded").classes("text-gray-500")
    
    def _rebuild_context(self):
        """Rebuild context display."""
        state = get_state()
        self.context_container.clear()
        
        if not state.last_context:
            with self.context_container:
                ui.label("No context captured yet. Send a message to see the context.").classes("text-gray-500")
            return
        
        with self.context_container:
            for msg in state.last_context:
                self._render_context_message(self.context_container, msg)
    
    def _scroll_to_bottom(self, element):
        """Scroll a container element to the bottom."""
        ui.run_javascript(f'document.querySelector("[id=\\"c{element.id}\\"]").scrollTop = 999999;')
    
    def _refresh_ui(self):
        """Refresh UI with current state.
        
        Updates labels every tick. Rebuilds panels only when data version changes.
        """
        state = get_state()
        
        # Update status chip (lightweight)
        status_colors = {"idle": "green", "thinking": "blue", "tool_call": "orange"}
        self.status_chip.text = state.status
        if state.current_tool:
            self.status_chip.text = f"{state.status}: {state.current_tool}"
        self.status_chip._props["color"] = status_colors.get(state.status, "gray")
        self.status_chip.update()
        
        # Update stats label (lightweight)
        self.stats_label.text = f"Messages: {len(state.messages)} | History: {state.total_messages} | Archival: {state.archival_count}"
        
        # Update context token info (lightweight)
        if state.last_context_time:
            time_str = state.last_context_time.strftime("%H:%M:%S")
            self.context_info.text = f"{state.last_context_tokens:,} tokens @ {time_str}"
            self.context_info.update()
        
        # Rebuild panels only if data changed
        if state.version != self._last_version:
            self._last_version = state.version
            self._load_initial_data()
            self._scroll_to_bottom(self.messages_scroll)
            self._scroll_to_bottom(self.context_scroll)
    
    def run(self):
        """Run the console server."""
        logger.info(f"Starting Lethe Console on port {self.port}")
        ui.run(
            port=self.port,
            title="Lethe Console",
            favicon="🧠",
            show=False,
            reload=False,  # Can't use reload in background thread
        )


async def run_console(port: int = 8080):
    """Run console in background."""
    console = ConsoleUI(port=port)
    import threading
    thread = threading.Thread(target=console.run, daemon=True)
    thread.start()
    logger.info(f"Lethe Console started on http://localhost:{port}")
