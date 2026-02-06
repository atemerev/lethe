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

# Role colors and styling
ROLE_STYLES = {
    "user": {"bg": "bg-blue-900", "border": "border-blue-500", "icon": "person", "align": "ml-8"},
    "assistant": {"bg": "bg-green-900", "border": "border-green-500", "icon": "smart_toy", "align": "mr-8"},
    "tool": {"bg": "bg-yellow-900", "border": "border-yellow-500", "icon": "build", "align": "mx-4"},
    "system": {"bg": "bg-gray-800", "border": "border-gray-500", "icon": "settings", "align": "mx-4"},
}


class ConsoleUI:
    """Mind state visualization console."""
    
    def __init__(self, port: int = 8080):
        self.port = port
        self._setup_ui()
    
    def _setup_ui(self):
        """Set up the UI layout."""
        
        @ui.page("/")
        async def main_page():
            state = get_state()
            
            # Dark theme
            ui.dark_mode().enable()
            
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
            
            # Header
            with ui.header().classes("bg-primary"):
                ui.label("🧠 Lethe Console").classes("text-h5 text-white")
                ui.space()
                self.status_chip = ui.chip("idle", icon="circle", color="green").classes("text-white")
                self.stats_label = ui.label("").classes("text-white ml-4")
            
            # Main layout - tabs
            with ui.tabs().classes("w-full") as tabs:
                messages_tab = ui.tab("Messages", icon="chat")
                memory_tab = ui.tab("Memory", icon="psychology")
                context_tab = ui.tab("Context", icon="code")
            
            with ui.tab_panels(tabs, value=messages_tab).classes("w-full"):
                # Messages panel
                with ui.tab_panel(messages_tab):
                    self.messages_container = ui.column().classes("w-full p-2 gap-1")
                
                # Memory panel
                with ui.tab_panel(memory_tab):
                    self.blocks_container = ui.column().classes("w-full p-2")
                
                # Context panel
                with ui.tab_panel(context_tab):
                    with ui.row().classes("w-full items-center mb-2 p-2"):
                        ui.label("Last context sent to LLM").classes("text-h6")
                        self.context_info = ui.chip("", icon="token").classes("ml-4")
                    self.context_container = ui.column().classes("w-full p-2 gap-1")
            
            # Initial data load
            self._load_initial_data()
            
            # Start refresh timer
            ui.timer(REFRESH_INTERVAL, self._refresh_ui)
    
    def _render_message(self, container, role: str, content: str):
        """Render a single message block."""
        style = ROLE_STYLES.get(role, ROLE_STYLES["system"])
        
        display_content = content if isinstance(content, str) else str(content)
        
        with container:
            with ui.card().classes(f"w-full {style['bg']} message-block {style['border']}"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(style["icon"]).classes("text-lg")
                    ui.label(role.upper()).classes("font-bold text-sm")
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
        
        # Render messages
        self.messages_container.clear()
        for msg in state.messages[-30:]:
            self._render_message(
                self.messages_container,
                msg.get("role", "?"),
                msg.get("content", "")
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
    
    def _refresh_ui(self):
        """Refresh UI with current state."""
        state = get_state()
        
        # Update status chip
        status_colors = {"idle": "green", "thinking": "blue", "tool_call": "orange"}
        self.status_chip.text = state.status
        if state.current_tool:
            self.status_chip.text = f"{state.status}: {state.current_tool}"
        self.status_chip._props["color"] = status_colors.get(state.status, "gray")
        self.status_chip.update()
        
        # Update stats
        self.stats_label.text = f"Messages: {len(state.messages)} | History: {state.total_messages} | Archival: {state.archival_count}"
        
        # Update context info and rebuild context view
        if state.last_context_time:
            time_str = state.last_context_time.strftime("%H:%M:%S")
            self.context_info.text = f"{state.last_context_tokens:,} tokens @ {time_str}"
            self.context_info.update()
            
            # Rebuild context view if we have new context
            if state.last_context:
                self._rebuild_context()
    
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
