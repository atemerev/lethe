"""Lethe Agent - Local agent with memory and tool execution.

Uses the local memory layer (LanceDB) and direct LLM calls.
Tools are just Python functions - no complex registration or approval loops.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional, Any

from lethe.config import Settings, get_settings
from lethe.memory import MemoryStore, AsyncLLMClient, LLMConfig, Hippocampus
from lethe.prompts import load_prompt_template
from lethe.tools import get_all_tools, function_to_schema

logger = logging.getLogger(__name__)


class Agent:
    """Lethe agent with local memory and direct LLM calls.
    
    Architecture:
    - Memory: LanceDB (blocks, archival, messages)
    - LLM: OpenRouter (Kimi K2.5 by default)
    - Tools: Python functions, schemas auto-generated
    """
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        
        # Initialize memory store
        self.memory = MemoryStore(
            data_dir=str(self.settings.memory_dir),
            workspace_dir=str(self.settings.workspace_dir),
            config_dir=str(self.settings.lethe_config_dir),
        )
        
        # Initialize LLM client (provider auto-detected from env vars)
        # Only pass model if explicitly set in env/settings (otherwise use provider default)
        llm_config = LLMConfig(
            provider=os.environ.get("LLM_PROVIDER", ""),  # Empty = auto-detect
            model=self.settings.llm_model,  # Empty = use provider default
            model_aux=self.settings.llm_model_aux,  # Empty = use provider default aux
            api_base=self.settings.llm_api_base,  # Custom API URL for local providers
            context_limit=self.settings.llm_context_limit,
        )
        
        # Load system prompt from config
        system_prompt = self._build_system_prompt()
        
        # Load model-specific communication rules into system prompt
        comm_rules = self._load_communication_rules(llm_config.model)
        if comm_rules:
            system_prompt += "\n\n" + comm_rules
        
        # Get memory context
        memory_context = self.memory.get_context_for_prompt()
        
        # Persistence callback for tool messages
        def persist_message(role: str, content, metadata: dict = None):
            self.memory.messages.add(role, content, metadata=metadata)
        
        self.llm = AsyncLLMClient(
            config=llm_config,
            system_prompt=system_prompt,
            memory_context=memory_context,
            on_message_persist=persist_message,
            usage_scope="cortex",
        )
        
        # Initialize hippocampus with LLM functions (analyzer + summarizer use aux model)
        hippocampus_enabled = os.environ.get("HIPPOCAMPUS_ENABLED", "true").lower() == "true"
        self.hippocampus = Hippocampus(
            self.memory, 
            summarizer=self._summarize_memories,
            analyzer=self._summarize_memories,  # Same aux model for analysis
            enabled=hippocampus_enabled,
        )
        
        # Add internal memory tools
        self._add_memory_tools()
        
        # Add external tools (file, bash, browser)
        self.llm.add_tools(get_all_tools())
        
        # For non-Anthropic models: embed tool reference in system prompt
        # (Kimi K2.5 needs tools visible in context text, not just tools parameter)
        if "claude" not in llm_config.model.lower() and "anthropic" not in llm_config.model.lower():
            self.llm.context._tool_reference = self.llm.context._build_tool_reference(self.llm.tools)
            logger.info(f"Embedded tool reference in system prompt ({len(self.llm.context._tool_reference)} chars)")
        
        # Note: call await agent.initialize() after creation to load message history
        self._initialized = False
        
        logger.info(f"Agent initialized with model {self.settings.llm_model}")
    
    async def initialize(self):
        """Async initialization - load message history with summarization."""
        if self._initialized:
            return
        await self._load_message_history()
        self._initialized = True
    
    async def _load_message_history(self):
        """Load recent message history into LLM context.
        
        Uses configurable two-tier loading:
        1. Load last N messages verbatim (LLM_MESSAGES_LOAD, default 20)
        2. Summarize M messages before that (LLM_MESSAGES_SUMMARIZE, default 100)
        """
        load_count = self.settings.llm_messages_load
        summarize_count = self.settings.llm_messages_summarize
        total_needed = load_count + summarize_count
        
        # Get all messages we need (get_recent returns oldest-first)
        all_messages = self.memory.messages.get_recent(total_needed)
        logger.info(f"Found {len(all_messages) if all_messages else 0} messages in database (requested {total_needed})")
        if not all_messages:
            return
        
        # Split into messages to summarize and messages to load verbatim
        if len(all_messages) > load_count:
            to_summarize = all_messages[:-load_count]
            to_load = all_messages[-load_count:]
        else:
            to_summarize = []
            to_load = all_messages
        
        # Summarize older messages if any
        if to_summarize:
            summary = await self._summarize_message_history(to_summarize)
            if summary:
                self.llm.context.summary = summary
                logger.info(f"Summarized {len(to_summarize)} older messages")
        
        # Load recent messages verbatim
        if to_load:
            self.llm.load_messages(to_load)
            logger.info(f"Loaded {len(to_load)} messages from history")
    
    async def _summarize_message_history(self, messages: list) -> str:
        """Summarize a list of messages using aux model."""
        # Format messages for summarization
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Handle multimodal content - extract text only
            if isinstance(content, list):
                text_parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text_parts.append(p.get("text", ""))
                    elif isinstance(p, dict) and p.get("type") == "image_url":
                        text_parts.append("[image]")
                content = " ".join(text_parts)
            
            # Skip base64 content and huge messages
            if "base64" in content or len(content) > 10000:
                content = f"[large content skipped: {len(content)} chars]"
            
            formatted.append(f"{role}: {content[:500]}")
        
        messages_text = "\n".join(formatted)
        summarize_tpl = load_prompt_template(
            "agent_history_summary",
            fallback="Summarize conversation:\n{messages_text}\n\nSummary:",
        )
        prompt = summarize_tpl.format(messages_text=messages_text)
        
        try:
            summary = await self.llm.complete(prompt, use_aux=True, usage_tag="history_summary")
            return summary.strip() if summary else ""
        except Exception as e:
            logger.warning(f"Failed to summarize history: {e}")
            return ""
    
    def _load_communication_rules(self, model: str) -> str:
        """Load model-specific communication rules from skills directory."""
        from pathlib import Path
        
        workspace = self.settings.workspace_dir
        skills_dir = workspace / "skills"
        
        # Determine which rules to load based on model name
        model_lower = model.lower()
        if "kimi" in model_lower:
            rules_file = skills_dir / "communication-kimi.md"
        elif "claude" in model_lower or "anthropic" in model_lower:
            rules_file = skills_dir / "communication-anthropic.md"
        else:
            # Try generic, then kimi as fallback (most restrictive)
            rules_file = skills_dir / "communication.md"
            if not rules_file.exists():
                rules_file = skills_dir / "communication-kimi.md"
        
        if rules_file.exists():
            content = rules_file.read_text().strip()
            logger.info(f"Loaded communication rules from {rules_file.name}")
            return content
        
        logger.debug("No communication rules found in skills/")
        return ""
    
    def _build_system_prompt(self) -> str:
        """Build system prompt from identity memory block.
        
        Identity block should contain:
        - Who you are (role, name, background)
        - Behavioral cues and roleplay instructions  
        - Communication style and output format rules
        """
        # Load identity from memory block (preferred)
        identity_block = self.memory.blocks.get("identity")
        if identity_block:
            return identity_block.get("value", "") or ""
        
        # Fallback to legacy persona block
        persona_block = self.memory.blocks.get("persona")
        if persona_block:
            logger.warning("Using legacy 'persona' block as system prompt. Consider migrating to 'identity' block (second person).")
            return persona_block.get("value", "") or ""
        
        # Final fallback if no identity or persona block exists
        logger.warning("No 'identity' or 'persona' memory block found, using minimal fallback")
        return load_prompt_template(
            "agent_system_fallback",
            fallback="You are an AI assistant with persistent memory.",
        )
    
    async def _summarize_memories(self, prompt: str) -> str:
        """Summarize memories using LLM (for hippocampus)."""
        return await self.llm.complete(prompt, use_aux=True, usage_tag="hippocampus")
    
    def _add_memory_tools(self):
        """Add internal memory management tools."""
        # Simple tool definitions - schemas auto-generated from docstrings
        
        def memory_read(label: str) -> str:
            """Read a memory block by label (with line numbers for editing).
            
            Args:
                label: Block label to read (e.g., 'persona', 'human', 'project')
            """
            block = self.memory.blocks.get_by_label(label)
            if block:
                value = block['value']
                # Add line numbers for editing reference
                lines = value.split('\n')
                numbered = [f"{i+1}→ {line}" for i, line in enumerate(lines)]
                return f"[{label}] ({len(value)} chars)\n" + '\n'.join(numbered)
            return f"Block '{label}' not found"
        
        def memory_update(label: str, value: str) -> str:
            """Update a memory block's value.
            
            Args:
                label: Block label to update
                value: New value for the block
            """
            try:
                if self.memory.blocks.update(label, value=value):
                    self.llm.update_memory_context(self.memory.get_context_for_prompt())
                    return f"Updated block '{label}'"
                return f"Block '{label}' not found"
            except Exception as e:
                return f"Error: {e}"
        
        def memory_append(label: str, text: str) -> str:
            """Append text to a memory block.
            
            Args:
                label: Block label to append to
                text: Text to append
            """
            try:
                if self.memory.blocks.append(label, text):
                    self.llm.update_memory_context(self.memory.get_context_for_prompt())
                    return f"Appended to block '{label}'"
                return f"Block '{label}' not found"
            except Exception as e:
                return f"Error: {e}"
        
        def archival_search(query: str, limit: int = 10) -> str:
            """Search long-term archival memory.
            
            Args:
                query: Search query
                limit: Max results (default 10)
            """
            results = self.memory.archival.search(query, limit=limit)
            if not results:
                return "No results found"
            
            output = []
            for i, r in enumerate(results, 1):
                text = r['text']
                if isinstance(text, str):
                    lines = text.split("\n")
                    if len(lines) > 50:
                        text = "\n".join(lines[:50]) + f"\n[... {len(lines) - 50} more lines]"
                output.append(f"{i}. [{r['score']:.2f}] {text}")
            return "\n".join(output)
        
        def archival_insert(text: str) -> str:
            """Store information in long-term archival memory.
            
            Args:
                text: Text to store
            """
            mem_id = self.memory.archival.add(text)
            return f"Stored in archival memory (id: {mem_id})"
        
        def conversation_search(query: str, limit: int = 10, role: str = "") -> str:
            """Search conversation history.
            
            Args:
                query: Search query
                limit: Max results (default 10)
                role: Filter by role (user, assistant) - optional
            """
            if role:
                results = self.memory.messages.search_by_role(query, role, limit=limit)
            else:
                results = self.memory.messages.search(query, limit=limit)
            
            if not results:
                return "No matching messages found"
            
            output = []
            for r in results:
                timestamp = r['created_at'][:16].replace('T', ' ')
                content = r['content']
                # Trim oversized entries (tool results can contain nested conversation dumps)
                if isinstance(content, str):
                    lines = content.split("\n")
                    if len(lines) > 50:
                        content = "\n".join(lines[:50]) + f"\n[... {len(lines) - 50} more lines]"
                output.append(f"[{timestamp}] {r['role']}: {content}")
            
            return f"Found {len(results)} messages:\n\n" + "\n\n".join(output)
        
        def send_image(file_path: str) -> dict:
            """Send an image to the user via Telegram.
            
            Use this to send screenshots, generated images, or any image file.
            The image will be sent in the correct order with your response.
            
            Args:
                file_path: Path to the image file to send
            """
            import os
            if not os.path.exists(file_path):
                return {"status": "error", "message": f"File not found: {file_path}"}
            
            # Return with _image_attachment to trigger on_image callback
            return {
                "status": "ok",
                "message": f"Image queued: {file_path}",
                "_image_attachment": {"path": file_path}
            }
        
        def view_image(file_path: str, max_size: int = 1568) -> dict:
            """View an image file - the image will be shown to you in context.
            
            Use this to look at images on disk, screenshots you took, or images you generated.
            The image will be injected into the conversation so you can see and analyze it.
            
            Args:
                file_path: Path to the image file to view
                max_size: Max dimension in pixels (default 1568, Anthropic recommended)
            """
            import os
            import base64
            from io import BytesIO
            
            if not os.path.exists(file_path):
                return {"status": "error", "message": f"File not found: {file_path}"}
            
            # Only image formats supported by LLM APIs
            ext = file_path.lower().split('.')[-1]
            mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 
                        'gif': 'image/gif', 'webp': 'image/webp'}
            
            if ext not in mime_map:
                return {"status": "error", "message": f"Not an image or unsupported format: {ext}. Use: jpg, png, gif, webp"}
            
            try:
                # Try to resize with PIL if available
                try:
                    from PIL import Image
                    
                    with Image.open(file_path) as img:
                        orig_size = f"{img.width}x{img.height}"
                        
                        # Resize if larger than max_size
                        if img.width > max_size or img.height > max_size:
                            ratio = min(max_size / img.width, max_size / img.height)
                            new_size = (int(img.width * ratio), int(img.height * ratio))
                            img = img.resize(new_size, Image.Resampling.LANCZOS)
                            resized = f" (resized from {orig_size} to {img.width}x{img.height})"
                        else:
                            resized = ""
                        
                        # Convert to bytes - JPEG for most, PNG for transparency
                        buffer = BytesIO()
                        if ext == 'png' and img.mode == 'RGBA':
                            img.save(buffer, format='PNG', optimize=True)
                            mime_type = 'image/png'
                        else:
                            if img.mode in ('RGBA', 'P'):
                                img = img.convert('RGB')
                            img.save(buffer, format='JPEG', quality=85, optimize=True)
                            mime_type = 'image/jpeg'
                        
                        image_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
                        
                except ImportError:
                    # PIL not available - read raw file
                    mime_type = mime_map[ext]
                    with open(file_path, 'rb') as f:
                        image_data = base64.b64encode(f.read()).decode('utf-8')
                    resized = ""
                
                # Check encoded size
                if len(image_data) > 5_000_000:
                    return {"status": "error", "message": f"Image too large: {len(image_data)//1_000_000}MB (max 5MB)"}
                
                return {
                    "status": "ok",
                    "message": f"Viewing image: {file_path}{resized}",
                    "_image_view": {
                        "path": file_path,
                        "mime_type": mime_type,
                        "data": image_data
                    }
                }
            except Exception as e:
                return {"status": "error", "message": f"Failed to read image: {e}"}
        
        def list_tools() -> str:
            """List all available tools grouped by category.
            
            Returns a summary of all tools the agent can use.
            """
            tools = self.llm._tools
            
            # Categorize tools
            categories = {
                "Memory": ["memory_read", "memory_update", "memory_append", "archival_search", "archival_insert", "conversation_search"],
                "Telegram": ["telegram_react", "telegram_send_message", "telegram_send_file", "send_image"],
                "Images": ["view_image", "send_image"],
                "Browser": ["browser_open", "browser_close", "browser_snapshot", "browser_click", "browser_type", "browser_scroll", "browser_screenshot", "browser_url"],
                "File": ["read_file", "write_file", "list_directory"],
                "CLI": ["run_command", "check_tool"],
                "Todo": ["todo_create", "todo_list", "todo_search", "todo_complete", "todo_remind_check", "todo_reminded"],
            }
            
            output = ["# Available Tools\n"]
            
            for category, tool_names in categories.items():
                available = [name for name in tool_names if name in tools]
                if available:
                    output.append(f"## {category}")
                    for name in available:
                        func, schema = tools[name]
                        desc = schema.get("description", "")[:60] if schema else ""
                        output.append(f"- `{name}`: {desc}...")
                    output.append("")
            
            # Other tools not in categories
            categorized = set(sum(categories.values(), []))
            other = [name for name in tools if name not in categorized]
            if other:
                output.append("## Other")
                for name in other:
                    func, schema = tools[name]
                    desc = schema.get("description", "")[:60] if schema else ""
                    output.append(f"- `{name}`: {desc}...")
            
            return "\n".join(output)
        
        # Add memory tools (keep minimal — too many tools overwhelms some models)
        for func in [memory_read, memory_update, memory_append, 
                     archival_search, archival_insert, conversation_search,
                     send_image, view_image, list_tools]:
            self.llm.add_tool(func)
    
    def add_tool(self, func: Callable):
        """Add a custom tool function."""
        self.llm.add_tool(func)
    
    async def chat(
        self,
        message: str,
        on_message: Optional[Callable[[str], Any]] = None,
        on_image: Optional[Callable[[str], Any]] = None,
        use_hippocampus: bool = True,
    ) -> str:
        """Send a message and get a response.
        
        Args:
            message: User message
            on_message: Optional callback for intermediate messages
            on_image: Optional callback for image attachments (screenshots)
            use_hippocampus: Whether to augment with recalled memories (default True)
            
        Returns:
            Final assistant response
        """
        # Store user message in history (original, without recall)
        self.memory.messages.add("user", message)
        
        # Recall relevant memories (unless disabled)
        recall_context = None
        if use_hippocampus:
            recent = self.memory.messages.get_recent(10)
            recall_context = await self.hippocampus.recall(message, recent)
        
        # Inject recall as transient system context for this turn.
        # Recall is synthetic context, not an assistant utterance.
        if recall_context:
            recall_ts = datetime.now(timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")
            self.llm.context.transient_system_context = (
                f"<recall_block source=\"hippocampus\" timestamp=\"{recall_ts}\">\n"
                f"{recall_context}\n"
                "</recall_block>"
            )
        
        try:
            # Get response from LLM (handles tool calls internally)
            response = await self.llm.chat(message, on_message=on_message, on_image=on_image)
        finally:
            # Never carry transient per-turn context into subsequent turns.
            self.llm.context.transient_system_context = ""
        
        # Store assistant response in history
        self.memory.messages.add("assistant", response)
        
        # Notify console of idle status
        self.llm._notify_status("idle")
        
        return response
    
    async def heartbeat(self, message: str) -> str:
        """Process heartbeat with minimal context and aux model.
        
        Uses lightweight context (no full identity, limited history) and
        aux model for cost efficiency.
        
        Args:
            message: Heartbeat message
            
        Returns:
            Response string
        """
        return await self.llm.heartbeat(message)
    
    async def close(self):
        """Clean up resources."""
        await self.llm.close()
    
    def get_stats(self) -> dict:
        """Get agent statistics."""
        return {
            "model": self.settings.llm_model,
            "memory_blocks": len(self.memory.blocks.list_blocks()),
            "archival_memories": self.memory.archival.count(),
            "message_history": self.memory.messages.count(),
            "total_messages": self.memory.messages.count(),  # Alias for console
            "tools": len(self.llm._tools),
            "llm": self.llm.get_context_stats(),
        }
    
    def refresh_memory_context(self):
        """Refresh LLM memory context from current blocks."""
        self.llm.update_memory_context(self.memory.get_context_for_prompt())
    
    def set_console_hooks(
        self,
        on_context_build: Optional[Callable] = None,
        on_status_change: Optional[Callable] = None,
        on_memory_change: Optional[Callable] = None,
        on_token_usage: Optional[Callable] = None,
    ):
        """Set callbacks for console state updates."""
        self._console_hooks = {
            "on_context_build": on_context_build,
            "on_status_change": on_status_change,
            "on_memory_change": on_memory_change,
            "on_token_usage": on_token_usage,
        }
        # Pass hooks to LLM client
        self.llm.set_console_hooks(on_context_build, on_status_change, on_token_usage)
