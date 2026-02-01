"""Lethe Agent - Local agent with memory and tool execution.

Uses the local memory layer (LanceDB) and direct LLM calls (OpenRouter).
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Any

from lethe.config import Settings, get_settings, load_config_file
from lethe.memory import MemoryStore, AsyncLLMClient, LLMConfig

logger = logging.getLogger(__name__)


class Agent:
    """Lethe agent with local memory and direct LLM calls.
    
    Architecture:
    - Memory: LanceDB (blocks, archival, messages)
    - LLM: OpenRouter (Kimi K2.5 by default)
    - Tools: Registered handlers executed locally
    """
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        
        # Initialize memory store
        self.memory = MemoryStore(data_dir=str(self.settings.memory_dir))
        
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
        
        # Register internal tools
        self._register_internal_tools()
        
        logger.info(f"Agent initialized with model {self.settings.llm_model}")
    
    def _build_system_prompt(self) -> str:
        """Build system prompt from config files."""
        identity = load_config_file("identity", self.settings)
        tools_doc = load_config_file("tools", self.settings)
        
        prompt = identity or "You are Lethe, an autonomous AI assistant."
        
        if tools_doc:
            prompt += f"\n\n## Available Tools\n{tools_doc}"
        
        return prompt
    
    def _register_internal_tools(self):
        """Register internal tools for memory management."""
        # Memory block tools
        self.llm.register_tool(
            "memory_read",
            self._tool_memory_read,
            {
                "name": "memory_read",
                "description": "Read a memory block by label (e.g., 'persona', 'human', 'project')",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Block label to read"}
                    },
                    "required": ["label"]
                }
            }
        )
        
        self.llm.register_tool(
            "memory_update",
            self._tool_memory_update,
            {
                "name": "memory_update",
                "description": "Update a memory block's value",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Block label to update"},
                        "value": {"type": "string", "description": "New value for the block"}
                    },
                    "required": ["label", "value"]
                }
            }
        )
        
        self.llm.register_tool(
            "memory_append",
            self._tool_memory_append,
            {
                "name": "memory_append",
                "description": "Append text to a memory block",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Block label to append to"},
                        "text": {"type": "string", "description": "Text to append"}
                    },
                    "required": ["label", "text"]
                }
            }
        )
        
        # Archival memory tools
        self.llm.register_tool(
            "archival_search",
            self._tool_archival_search,
            {
                "name": "archival_search",
                "description": "Search long-term archival memory using semantic/hybrid search",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results (default 10)"}
                    },
                    "required": ["query"]
                }
            }
        )
        
        self.llm.register_tool(
            "archival_insert",
            self._tool_archival_insert,
            {
                "name": "archival_insert",
                "description": "Store information in long-term archival memory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to store"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for categorization"
                        }
                    },
                    "required": ["text"]
                }
            }
        )
        
        # Conversation search tool
        self.llm.register_tool(
            "conversation_search",
            self._tool_conversation_search,
            {
                "name": "conversation_search",
                "description": "Search conversation history using semantic/hybrid search. Find past discussions, decisions, and context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results (default 10)"},
                        "role": {"type": "string", "description": "Filter by role (user, assistant)"}
                    },
                    "required": ["query"]
                }
            }
        )
        
        logger.info("Registered internal memory tools")
    
    # Tool implementations
    def _tool_memory_read(self, label: str) -> str:
        """Read a memory block."""
        block = self.memory.blocks.get_by_label(label)
        if block:
            return f"[{label}]\n{block['value']}"
        return f"Block '{label}' not found"
    
    def _tool_memory_update(self, label: str, value: str) -> str:
        """Update a memory block."""
        try:
            if self.memory.blocks.update(label, value=value):
                # Refresh LLM's memory context
                self.llm.update_memory_context(self.memory.get_context_for_prompt())
                return f"Updated block '{label}'"
            return f"Block '{label}' not found"
        except Exception as e:
            return f"Error updating block: {e}"
    
    def _tool_memory_append(self, label: str, text: str) -> str:
        """Append to a memory block."""
        try:
            if self.memory.blocks.append(label, text):
                self.llm.update_memory_context(self.memory.get_context_for_prompt())
                return f"Appended to block '{label}'"
            return f"Block '{label}' not found"
        except Exception as e:
            return f"Error appending to block: {e}"
    
    def _tool_archival_search(self, query: str, limit: int = 10) -> str:
        """Search archival memory."""
        results = self.memory.archival.search(query, limit=limit)
        if not results:
            return "No results found"
        
        output = []
        for i, r in enumerate(results, 1):
            output.append(f"{i}. [{r['score']:.2f}] {r['text']}")
        return "\n".join(output)
    
    def _tool_archival_insert(self, text: str, tags: Optional[list] = None) -> str:
        """Insert into archival memory."""
        mem_id = self.memory.archival.add(text, tags=tags)
        return f"Stored in archival memory (id: {mem_id})"
    
    def _tool_conversation_search(self, query: str, limit: int = 10, role: Optional[str] = None) -> str:
        """Search conversation history."""
        if role:
            results = self.memory.messages.search_by_role(query, role, limit=limit)
        else:
            results = self.memory.messages.search(query, limit=limit)
        
        if not results:
            return "No matching messages found"
        
        output = []
        for r in results:
            score = r.get('score', 0)
            timestamp = r['created_at'][:16].replace('T', ' ')
            content = r['content'][:200] + "..." if len(r['content']) > 200 else r['content']
            output.append(f"[{timestamp}] {r['role']}: {content}")
        
        return f"Found {len(results)} messages:\n\n" + "\n\n".join(output)
    
    def register_tool(self, name: str, handler: Callable, schema: dict):
        """Register an external tool."""
        self.llm.register_tool(name, handler, schema)
    
    async def chat(
        self,
        message: str,
        on_message: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Send a message and get a response.
        
        Args:
            message: User message
            on_message: Optional callback for intermediate messages
            
        Returns:
            Final assistant response
        """
        # Store user message in history
        self.memory.messages.add("user", message)
        
        # Get response from LLM (handles tool calls internally)
        response = await self.llm.chat(message, on_message=on_message)
        
        # Store assistant response in history
        self.memory.messages.add("assistant", response)
        
        return response
    
    async def close(self):
        """Clean up resources."""
        await self.llm.close()
    
    def get_stats(self) -> dict:
        """Get agent statistics."""
        llm_stats = self.llm.get_context_stats() if hasattr(self.llm, 'get_context_stats') else {}
        return {
            "model": self.settings.llm_model,
            "memory_blocks": len(self.memory.blocks.list_blocks()),
            "archival_memories": self.memory.archival.count(),
            "message_history": self.memory.messages.count(),
            "llm": llm_stats,
        }
    
    def initialize_default_blocks(self):
        """Initialize default memory blocks if they don't exist."""
        defaults = [
            ("persona", "Who I am and how I behave", "I am Lethe, an autonomous AI assistant."),
            ("human", "Information about my human", ""),
            ("project", "Current project context", ""),
        ]
        
        for label, description, default_value in defaults:
            if not self.memory.blocks.get_by_label(label):
                self.memory.blocks.create(
                    label=label,
                    value=default_value,
                    description=description,
                )
                logger.info(f"Created default block: {label}")
        
        # Refresh LLM context
        self.llm.update_memory_context(self.memory.get_context_for_prompt())
