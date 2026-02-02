"""Lethe Agent - Local agent with memory and tool execution.

Uses the local memory layer (LanceDB) and direct LLM calls.
Supports multiple providers including Claude Max subscription via OAuth.
Tools are just Python functions - no complex registration or approval loops.
"""

import asyncio
import logging
import os
from typing import Callable, Optional, Any

from lethe.config import Settings, get_settings
from lethe.memory import MemoryStore, AsyncLLMClient, LLMConfig, Hippocampus
from lethe.tools import get_all_tools, function_to_schema

logger = logging.getLogger(__name__)

# OAuth support (lazy import to avoid circular deps)
_oauth_instance = None
_oauth_send_message = None
_oauth_receive_message = None


def set_oauth_callbacks(
    send_message: Optional[Callable] = None,
    receive_message: Optional[Callable] = None,
):
    """Set callbacks for OAuth flow (called from main.py with Telegram functions)."""
    global _oauth_send_message, _oauth_receive_message
    _oauth_send_message = send_message
    _oauth_receive_message = receive_message


async def _get_oauth_token() -> str:
    """Get OAuth token for Claude Max, initializing if needed."""
    global _oauth_instance
    if _oauth_instance is None:
        from lethe.oauth import ensure_claude_max_auth
        _oauth_instance = await ensure_claude_max_auth(
            send_message=_oauth_send_message,
            receive_message=_oauth_receive_message,
        )
    return await _oauth_instance.get_access_token()


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
        # Check if using OAuth provider (claude-max)
        provider = os.environ.get("LLM_PROVIDER", "").lower()
        oauth_getter = _get_oauth_token if provider == "claude-max" else None
        
        # Only pass model if explicitly set in env (otherwise use provider default)
        explicit_model = os.environ.get("LLM_MODEL", "")
        
        llm_config = LLMConfig(
            provider=provider or "",  # Empty = auto-detect
            model=explicit_model,  # Empty = use provider default
            context_limit=self.settings.llm_context_limit,
            oauth_token_getter=oauth_getter,
        )
        
        # Load system prompt from config
        system_prompt = self._build_system_prompt()
        
        # Get memory context
        memory_context = self.memory.get_context_for_prompt()
        
        self.llm = AsyncLLMClient(
            config=llm_config,
            system_prompt=system_prompt,
            memory_context=memory_context,
        )
        
        # Initialize hippocampus with summarizer
        hippocampus_enabled = os.environ.get("HIPPOCAMPUS_ENABLED", "true").lower() == "true"
        self.hippocampus = Hippocampus(
            self.memory, 
            summarizer=self._summarize_memories,
            enabled=hippocampus_enabled,
        )
        
        # Add internal memory tools
        self._add_memory_tools()
        
        # Add external tools (file, bash, browser)
        self.llm.add_tools(get_all_tools())
        
        # Load recent message history into context
        self._load_message_history()
        
        logger.info(f"Agent initialized with model {self.settings.llm_model}")
    
    def _load_message_history(self, max_messages: int = 50):
        """Load recent message history into LLM context.
        
        This gives the agent context from previous conversations.
        """
        recent = self.memory.messages.get_recent(max_messages)
        if recent:
            # Reverse to get chronological order (oldest first)
            recent = list(reversed(recent))
            self.llm.load_messages(recent)
            logger.info(f"Loaded {len(recent)} messages from history")
    
    def _build_system_prompt(self) -> str:
        """Build system prompt from persona seed block."""
        persona_path = self.settings.lethe_config_dir / "blocks" / "persona.md"
        
        if persona_path.exists():
            return persona_path.read_text()
        
        return "You are Lethe, an autonomous AI assistant."
    
    async def _summarize_memories(self, prompt: str) -> str:
        """Summarize memories using LLM (for hippocampus)."""
        return await self.llm.complete(prompt)
    
    def _add_memory_tools(self):
        """Add internal memory management tools."""
        # Simple tool definitions - schemas auto-generated from docstrings
        
        def memory_read(label: str) -> str:
            """Read a memory block by label.
            
            Args:
                label: Block label to read (e.g., 'persona', 'human', 'project')
            """
            block = self.memory.blocks.get_by_label(label)
            if block:
                return f"[{label}]\n{block['value']}"
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
                output.append(f"{i}. [{r['score']:.2f}] {r['text']}")
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
                content = r['content'][:200] + "..." if len(r['content']) > 200 else r['content']
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
        
        # Add all memory tools
        for func in [memory_read, memory_update, memory_append, 
                     archival_search, archival_insert, conversation_search,
                     send_image]:
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
        
        # Augment message with hippocampus recall (unless disabled)
        if use_hippocampus:
            recent = self.memory.messages.get_recent(10)
            augmented_message = await self.hippocampus.augment_message(message, recent)
        else:
            augmented_message = message
        
        # Get response from LLM (handles tool calls internally)
        response = await self.llm.chat(augmented_message, on_message=on_message, on_image=on_image)
        
        # Store assistant response in history
        self.memory.messages.add("assistant", response)
        
        return response
    
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
            "tools": len(self.llm._tools),
            "llm": self.llm.get_context_stats(),
        }
    
    def refresh_memory_context(self):
        """Refresh LLM memory context from current blocks."""
        self.llm.update_memory_context(self.memory.get_context_for_prompt())
