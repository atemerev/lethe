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
            
            # Header
            with ui.header().classes("bg-primary"):
                ui.label("Lethe Console").classes("text-h5 text-white")
                ui.space()
                self.status_label = ui.label("Status: initializing...").classes("text-white")
            
            # Main layout - two columns
            with ui.splitter(value=50).classes("w-full h-screen") as splitter:
                with splitter.before:
                    with ui.column().classes("w-full p-4"):
                        ui.label("💬 Messages").classes("text-h6")
                        self.messages_log = ui.log(max_lines=100).classes(
                            "w-full h-80 bg-gray-900"
                        )
                
                with splitter.after:
                    with ui.column().classes("w-full p-4"):
                        ui.label("🧠 Memory Blocks").classes("text-h6")
                        self.blocks_column = ui.column().classes("w-full")
            
            # Bottom panel: Context
            with ui.expansion("📤 Context Sent to LLM", icon="code").classes("w-full"):
                self.context_info = ui.label("No context yet").classes("text-caption")
                self.context_json = ui.code("[]", language="json").classes("w-full max-h-64 overflow-auto text-xs")
            
            # Initial data load
            self._load_initial_data()
            
            # Start refresh timer
            ui.timer(REFRESH_INTERVAL, self._refresh_ui)
        
        @ui.page("/api/state")
        async def api_state():
            """API endpoint for state (for external tools)."""
            state = get_state()
            return {
                "status": state.status,
                "current_tool": state.current_tool,
                "memory_blocks": list(state.memory_blocks.keys()),
                "message_count": len(state.messages),
                "last_context_tokens": state.last_context_tokens,
            }
    
    def _load_initial_data(self):
        """Load initial data into UI."""
        state = get_state()
        
        # Load messages into log
        for msg in state.messages[-50:]:
            role = msg.get("role", "?")
            content = str(msg.get("content", ""))[:200]
            self.messages_log.push(f"[{role}] {content}")
        
        # Build memory blocks UI
        self._rebuild_blocks()
    
    def _rebuild_blocks(self):
        """Rebuild memory blocks display."""
        state = get_state()
        self.blocks_column.clear()
        
        with self.blocks_column:
            # Identity
            if state.identity:
                with ui.expansion("identity", icon="person").classes("w-full"):
                    ui.markdown(f"```\n{state.identity[:2000]}\n```").classes("text-xs")
            
            # Summary
            if state.summary:
                with ui.expansion("summary", icon="summarize").classes("w-full"):
                    ui.markdown(f"```\n{state.summary[:1000]}\n```").classes("text-xs")
            
            # Other blocks
            for label, block in state.memory_blocks.items():
                if label == "identity":
                    continue  # Already shown above
                    
                value = block.get("value", "")
                chars = len(value)
                limit = block.get("limit", 20000)
                
                icon = "memory"
                if "persona" in label or "capabil" in label:
                    icon = "psychology"
                elif "human" in label:
                    icon = "person"
                elif "project" in label:
                    icon = "folder"
                elif "task" in label:
                    icon = "task"
                elif "tool" in label:
                    icon = "build"
                
                with ui.expansion(f"{label} ({chars}/{limit})", icon=icon).classes("w-full"):
                    # Use pre for better formatting
                    ui.html(f"<pre style='white-space:pre-wrap;font-size:11px;max-height:300px;overflow:auto'>{value[:3000]}</pre>")
            
            if not state.memory_blocks and not state.identity:
                ui.label("No memory blocks loaded").classes("text-gray-500")
    
    def _refresh_ui(self):
        """Refresh UI with current state."""
        state = get_state()
        
        # Update status bar
        status_text = f"Status: {state.status}"
        if state.current_tool:
            status_text += f" ({state.current_tool})"
        status_text += f" | Msgs: {len(state.messages)} | Total: {state.total_messages} | Archival: {state.archival_count}"
        self.status_label.text = status_text
        
        # Update context info
        if state.last_context_time:
            time_str = state.last_context_time.strftime("%H:%M:%S")
            self.context_info.text = f"{state.last_context_tokens} tokens @ {time_str}"
            
            # Format context for display (truncated)
            if state.last_context:
                formatted = []
                for msg in state.last_context[:10]:  # First 10 messages
                    role = msg.get("role", "?")
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        content = content[:500] + "..." if len(content) > 500 else content
                    elif isinstance(content, list):
                        content = f"[{len(content)} content blocks]"
                    formatted.append({"role": role, "content": content})
                
                if len(state.last_context) > 10:
                    formatted.append({"note": f"... and {len(state.last_context) - 10} more messages"})
                
                self.context_json.content = json.dumps(formatted, indent=2, default=str)
    
    def run(self):
        """Run the console server."""
        logger.info(f"Starting Lethe Console on port {self.port}")
        ui.run(
            port=self.port,
            title="Lethe Console",
            favicon="🧠",
            show=False,
            reload=False,
        )


async def run_console(port: int = 8080):
    """Run console in background."""
    console = ConsoleUI(port=port)
    import threading
    thread = threading.Thread(target=console.run, daemon=True)
    thread.start()
    logger.info(f"Lethe Console started on http://localhost:{port}")
