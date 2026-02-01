"""Lethe Agent - Local agent with memory and tool execution.

Uses the local memory layer (LanceDB) and direct LLM calls (OpenRouter).
Tools are just Python functions - no complex registration or approval loops.
"""

import asyncio
import logging
import os
from typing import Callable, Optional, Any

from lethe.config import Settings, get_settings, load_config_file
from lethe.memory import MemoryStore, AsyncLLMClient, LLMConfig
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
        )
        
        # Initialize LLM client
        llm_config = LLMConfig(
            model=self.settings.llm_model,
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            context_limit=self.settings.llm_context_limit,
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
        """Build system prompt from config files."""
        identity = load_config_file("identity", self.settings)
        tools_doc = load_config_file("tools", self.settings)
        
        prompt = identity or "You are Lethe, an autonomous AI assistant."
        
        if tools_doc:
            prompt += f"\n\n## Available Tools\n{tools_doc}"
        
        return prompt
    
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
    ) -> str:
        """Send a message and get a response.
        
        Args:
            message: User message
            on_message: Optional callback for intermediate messages
            on_image: Optional callback for image attachments (screenshots)
            
        Returns:
            Final assistant response
        """
        # Store user message in history
        self.memory.messages.add("user", message)
        
        # Get response from LLM (handles tool calls internally)
        response = await self.llm.chat(message, on_message=on_message, on_image=on_image)
        
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
    
    def initialize_default_blocks(self):
        """Initialize default memory blocks if they don't exist."""
        defaults = [
            ("persona", "Who I am and how I communicate. I evolve this as I develop my style.", 
             "I am Lethe, an autonomous AI assistant. I communicate naturally and adapt to my user."),
            ("human", "What I've learned about my human - their preferences, communication style, what matters to them. I update this to serve them better.", 
             ""),
            ("project", "Current project context and what we're working on together.", 
             ""),
        ]
        
        for label, description, default_value in defaults:
            try:
                if not self.memory.blocks.get_by_label(label):
                    self.memory.blocks.create(
                        label=label,
                        value=default_value,
                        description=description,
                    )
                    logger.info(f"Created default block: {label}")
            except ValueError:
                # Block already exists
                pass
        
        # Refresh LLM context
        self.llm.update_memory_context(self.memory.get_context_for_prompt())
