"""Main memory store coordinating all memory subsystems."""

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
    - blocks: Core memory as files in workspace (identity.md, human.md, project.md, etc.)
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
        
        # Create skills and projects directories
        skills_dir = self.workspace_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "projects").mkdir(parents=True, exist_ok=True)
        self._ensure_skills_bootstrap(skills_dir)
        
        # Copy workspace seed files (questions.md, etc.) if not present
        self._init_workspace_seeds(str(self.config_dir))
        
        # Initialize subsystems
        self.blocks = BlockManager(blocks_workspace)
        self.archival = ArchivalMemory(self.db)
        self.messages = MessageHistory(self.db)
        
        logger.info("Memory store initialized")
    
    def _init_workspace_seeds(self, config_dir: str = "config"):
        """Copy workspace seed files/directories to workspace if not present.

        Includes:
        - config/workspace/* -> workspace/*
        - config/prompts/* -> workspace/prompts/*
        """
        seeds_dir = Path(config_dir) / "workspace"
        if seeds_dir.exists():
            for seed_file in seeds_dir.rglob("*"):
                if not seed_file.is_file():
                    continue
                rel = seed_file.relative_to(seeds_dir)
                target = self.workspace_dir / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(seed_file.read_text())
                    logger.info(f"Initialized workspace file from seed: {rel}")

        prompts_dir = Path(config_dir) / "prompts"
        if prompts_dir.exists():
            for prompt_file in prompts_dir.rglob("*.md"):
                rel = prompt_file.relative_to(prompts_dir)
                target = self.workspace_dir / "prompts" / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(prompt_file.read_text())
                    logger.info(f"Initialized workspace prompt from seed: prompts/{rel}")

    def _ensure_skills_bootstrap(self, skills_dir: Path):
        """Ensure the skills directory always has a known entrypoint file."""
        readme = skills_dir / "README.md"
        if readme.exists():
            return

        readme.write_text(
            "# Skills\n\n"
            "This directory stores skill files with extended workflows and references.\n"
            "This README is intentionally always present so skills are discoverable.\n\n"
            "Use core tools to work with skills:\n"
            "- list_directory(\"~/lethe/skills/\")\n"
            "- read_file(\"~/lethe/skills/README.md\")\n"
            "- read_file(\"~/lethe/skills/<name>.md\")\n"
            "- grep_search(\"keyword\", path=\"~/lethe/skills/\")\n"
        )
        logger.info("Initialized default skills README")
    
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
    
    # Blocks that rarely change — eligible for long-lived cache (1h)
    STABLE_BLOCKS = {"human", "tools"}

    @staticmethod
    def _parse_iso_timestamp(raw: str):
        """Parse ISO timestamp safely."""
        from datetime import datetime
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _format_timestamp(dt) -> str:
        """Format timestamps with weekday and UTC marker."""
        from datetime import timezone
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%a %Y-%m-%d %H:%M:%S UTC")

    def _format_block(self, block: dict) -> str:
        """Format a single block for context."""
        label = block["label"]
        value = block["value"] or ""
        description = block.get("description") or ""
        limit = block.get("limit") or 20000
        created_at = self._parse_iso_timestamp(block.get("created_at", ""))
        updated_at = self._parse_iso_timestamp(block.get("updated_at", ""))
        
        lines = [
            f"<{label}>",
            "<description>",
            description,
            "</description>",
            "<metadata>",
            f"- chars={len(value)}/{limit}",
        ]
        if created_at:
            lines.append(f"- created_at={self._format_timestamp(created_at)}")
        if updated_at:
            lines.append(f"- updated_at={self._format_timestamp(updated_at)}")
        lines.extend([
            "</metadata>",
            "<value>",
            value,
            "</value>",
            f"</{label}>",
        ])
        return "\n".join(lines)

    def get_context_for_prompt(self, max_tokens: int = 8000) -> str:
        """Get formatted memory context for LLM prompt (single string).
        
        Used by non-Anthropic models (no cache splitting needed).
        """
        stable, volatile = self.get_context_split()
        parts = [p for p in [stable, volatile] if p]
        return "\n\n".join(parts)

    def get_context_split(self) -> tuple:
        """Get memory context split into (stable, volatile) for caching.
        
        - stable: blocks that rarely change (human, tools) — 1h cache
        - volatile: blocks that change often (project, tasks) + metadata — 5m cache
        
        Returns:
            (stable_str, volatile_str) — either can be empty string
        """
        from datetime import datetime, timezone
        
        blocks = self.blocks.list_blocks()
        stable_parts = []
        volatile_parts = []
        
        if blocks:
            stable_block_strs = []
            volatile_block_strs = []
            
            for block in blocks:
                if block.get("hidden"):
                    continue
                label = block["label"]
                if label == "identity":
                    continue
                
                formatted = self._format_block(block)
                if label in self.STABLE_BLOCKS:
                    stable_block_strs.append(formatted)
                else:
                    volatile_block_strs.append(formatted)
            
            if stable_block_strs:
                stable_parts.append(
                    "<memory_blocks_stable>\n" +
                    "\n\n".join(stable_block_strs) +
                    "\n</memory_blocks_stable>"
                )
            
            if volatile_block_strs:
                volatile_parts.append(
                    "<memory_blocks>\n" +
                    "\n\n".join(volatile_block_strs) +
                    "\n</memory_blocks>"
                )
        
        # Memory metadata (volatile — counts change)
        now = datetime.now(timezone.utc)
        message_count = self.messages.count()
        archival_count = self.archival.count()
        
        last_modified = None
        for block in blocks:
            if block.get("updated_at"):
                block_time = self._parse_iso_timestamp(block["updated_at"])
                if block_time and (last_modified is None or block_time > last_modified):
                    last_modified = block_time
        if last_modified is None:
            last_modified = now
        
        metadata_lines = [
            "<memory_metadata>",
            f"- now={self._format_timestamp(now)}",
            f"- memory_blocks_last_modified={self._format_timestamp(last_modified)}",
            f"- {message_count} previous messages between you and the user are stored in recall memory (use tools to access them)",
        ]
        if archival_count > 0:
            metadata_lines.append(f"- {archival_count} total memories you created are stored in archival memory (use tools to access them)")
        metadata_lines.append("</memory_metadata>")
        volatile_parts.append("\n".join(metadata_lines))
        
        return "\n\n".join(stable_parts), "\n\n".join(volatile_parts)
    
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
