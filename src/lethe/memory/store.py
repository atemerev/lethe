"""Main memory store coordinating all memory subsystems."""

import os
from pathlib import Path
from typing import Optional
import lancedb
import logging

logger = logging.getLogger(__name__)

from lethe.memory.blocks import BlockManager
from lethe.memory.archival import ArchivalMemory
from lethe.memory.messages import MessageHistory


class MemoryStore:
    """Unified memory store.
    
    Provides:
    - blocks: Core memory as files in workspace (persona.md, human.md, etc.)
    - archival: Long-term semantic memory with hybrid search (LanceDB)
    - messages: Conversation history (LanceDB)
    
    Blocks live in workspace for easy editing. Initialized from data/ templates.
    """
    
    def __init__(self, data_dir: str = "data/memory", workspace_dir: str = "workspace", config_dir: str = "config"):
        """Initialize memory store.
        
        Args:
            data_dir: Directory for persistent data (archival, messages)
            workspace_dir: Working directory for blocks (agent reads/writes here)
            config_dir: Directory with seed block templates
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        
        self.config_dir = Path(config_dir)
        
        # Connect to LanceDB (for archival and messages only)
        self.db = lancedb.connect(str(self.data_dir / "lancedb"))
        logger.info(f"Connected to LanceDB at {self.data_dir / 'lancedb'}")
        
        # Initialize blocks in workspace, copying from config/blocks/ seeds if needed
        blocks_workspace = self.workspace_dir / "memory"
        blocks_workspace.mkdir(parents=True, exist_ok=True)
        self._init_blocks_from_templates(blocks_workspace, str(self.config_dir))
        
        # Initialize subsystems
        self.blocks = BlockManager(blocks_workspace)
        self.archival = ArchivalMemory(self.db)
        self.messages = MessageHistory(self.db)
        
        logger.info("Memory store initialized")
    
    def _init_blocks_from_templates(self, blocks_workspace: Path, config_dir: str = "config"):
        """Copy block seeds from config/blocks/ to workspace if not present."""
        templates_dir = Path(config_dir) / "blocks"
        if not templates_dir.exists():
            logger.debug(f"No seed blocks found at {templates_dir}")
            return
        
        for template_file in templates_dir.glob("*.md"):
            target_file = blocks_workspace / template_file.name
            if not target_file.exists():
                # Copy content
                target_file.write_text(template_file.read_text())
                logger.info(f"Initialized block from seed: {template_file.name}")
                
                # Copy metadata if exists
                meta_file = template_file.with_suffix(".meta.json")
                if meta_file.exists():
                    target_meta = blocks_workspace / meta_file.name
                    target_meta.write_text(meta_file.read_text())
    
    def get_context_for_prompt(self, max_tokens: int = 8000) -> str:
        """Get formatted memory context for LLM prompt.
        
        Matches Letta's context engineering format:
        - Memory blocks with description, metadata, value
        - Memory metadata with timestamps and counts
        
        Args:
            max_tokens: Approximate max tokens for context
            
        Returns:
            Formatted string with all memory blocks
        """
        from datetime import datetime, timezone
        
        sections = []
        
        # Build memory blocks section (Letta-style)
        blocks = self.blocks.list_blocks()
        if blocks:
            block_lines = ["<memory_blocks>"]
            block_lines.append("The following memory blocks are currently engaged in your core memory unit:\n")
            
            for i, block in enumerate(blocks):
                if block.get("hidden"):
                    continue
                
                label = block["label"]
                
                # Skip identity block - it's used as system prompt, not in memory_blocks
                if label == "identity":
                    continue
                value = block["value"] or ""
                description = block.get("description") or ""
                limit = block.get("limit") or 20000
                
                block_lines.append(f"<{label}>")
                block_lines.append("<description>")
                block_lines.append(description)
                block_lines.append("</description>")
                block_lines.append("<metadata>")
                block_lines.append(f"- chars={len(value)}/{limit}")
                block_lines.append("</metadata>")
                block_lines.append("<value>")
                block_lines.append(value)
                block_lines.append("</value>")
                block_lines.append(f"</{label}>")
                
                if i < len(blocks) - 1:
                    block_lines.append("")
            
            block_lines.append("\n</memory_blocks>")
            sections.append("\n".join(block_lines))
        
        # Build memory metadata section
        now = datetime.now(timezone.utc)
        message_count = self.messages.count()
        archival_count = self.archival.count()
        
        # Get last modified time from blocks
        last_modified = now  # Default to now
        for block in blocks:
            if block.get("updated_at"):
                block_time = datetime.fromisoformat(block["updated_at"].replace("Z", "+00:00"))
                if block_time > last_modified:
                    last_modified = block_time
        
        metadata_lines = [
            "<memory_metadata>",
            f"- The current system date is: {now.strftime('%B %d, %Y')}",
            f"- Memory blocks were last modified: {last_modified.strftime('%Y-%m-%d %I:%M:%S %p')} UTC{last_modified.strftime('%z')}",
            f"- {message_count} previous messages between you and the user are stored in recall memory (use tools to access them)",
        ]
        
        if archival_count > 0:
            metadata_lines.append(f"- {archival_count} total memories you created are stored in archival memory (use tools to access them)")
        
        metadata_lines.append("</memory_metadata>")
        sections.append("\n".join(metadata_lines))
        
        return "\n\n".join(sections)
    
    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "hybrid"
    ) -> list[dict]:
        """Search across archival memory.
        
        Args:
            query: Search query
            limit: Max results
            search_type: "hybrid", "vector", or "fts"
            
        Returns:
            List of matching passages
        """
        return self.archival.search(query, limit=limit, search_type=search_type)
    
    def add_memory(self, text: str, metadata: Optional[dict] = None) -> str:
        """Add a memory to archival storage.
        
        Args:
            text: Memory text
            metadata: Optional metadata
            
        Returns:
            Memory ID
        """
        return self.archival.add(text, metadata=metadata)
    
    def get_recent_messages(self, limit: int = 20) -> list[dict]:
        """Get recent conversation messages.
        
        Args:
            limit: Max messages to return
            
        Returns:
            List of messages
        """
        return self.messages.get_recent(limit=limit)
    
    def add_message(self, role: str, content: str, metadata: Optional[dict] = None) -> str:
        """Add a message to history.
        
        Args:
            role: Message role (user, assistant, system, tool)
            content: Message content
            metadata: Optional metadata
            
        Returns:
            Message ID
        """
        return self.messages.add(role, content, metadata=metadata)
