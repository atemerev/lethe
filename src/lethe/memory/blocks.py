"""Memory blocks - Core memory that's always in context.

Blocks are stored as simple text files in a directory.
Each file is named after the block label (e.g., persona.md, human.md).
Metadata is stored in a companion .meta.json file.
"""

import json
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20000  # Characters


class BlockManager:
    """Manages memory blocks (core memory) as files.
    
    Blocks are key-value pairs that are always included in the LLM context.
    Each block is a .md file with optional .meta.json for metadata.
    """
    
    def __init__(self, blocks_dir: str | Path):
        """Initialize block manager.
        
        Args:
            blocks_dir: Directory containing block files
        """
        self.blocks_dir = Path(blocks_dir)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Block manager initialized at {self.blocks_dir}")
    
    def _block_path(self, label: str) -> Path:
        """Get path to block file."""
        return self.blocks_dir / f"{label}.md"
    
    def _meta_path(self, label: str) -> Path:
        """Get path to metadata file."""
        return self.blocks_dir / f"{label}.meta.json"
    
    def _load_meta(self, label: str) -> dict:
        """Load metadata for a block."""
        meta_path = self._meta_path(label)
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                pass
        return {}
    
    def _save_meta(self, label: str, meta: dict):
        """Save metadata for a block."""
        meta_path = self._meta_path(label)
        meta_path.write_text(json.dumps(meta, indent=2))
    
    def create(
        self,
        label: str,
        value: str = "",
        description: str = "",
        limit: int = DEFAULT_LIMIT,
        read_only: bool = False,
        hidden: bool = False,
    ) -> str:
        """Create a new memory block.
        
        Args:
            label: Block label (e.g., "persona", "human")
            value: Initial value
            description: Block description
            limit: Character limit
            read_only: Whether block is read-only
            hidden: Whether block is hidden from context
            
        Returns:
            Block label
        """
        block_path = self._block_path(label)
        if block_path.exists():
            raise ValueError(f"Block '{label}' already exists")
        
        if len(value) > limit:
            raise ValueError(f"Value length ({len(value)}) exceeds limit ({limit})")
        
        # Write content
        block_path.write_text(value)
        
        # Write metadata
        now = datetime.now(timezone.utc).isoformat()
        self._save_meta(label, {
            "description": description,
            "limit": limit,
            "read_only": read_only,
            "hidden": hidden,
            "created_at": now,
            "updated_at": now,
        })
        
        logger.info(f"Created block '{label}'")
        return label
    
    def get(self, label: str) -> Optional[dict]:
        """Get a block by label.
        
        Args:
            label: Block label
            
        Returns:
            Block dict or None
        """
        return self.get_by_label(label)
    
    def get_by_label(self, label: str) -> Optional[dict]:
        """Get a block by label.
        
        Args:
            label: Block label
            
        Returns:
            Block dict or None
        """
        block_path = self._block_path(label)
        if not block_path.exists():
            return None
        
        value = block_path.read_text()
        meta = self._load_meta(label)
        
        return {
            "label": label,
            "value": value,
            "description": meta.get("description", ""),
            "limit": meta.get("limit", DEFAULT_LIMIT),
            "read_only": meta.get("read_only", False),
            "hidden": meta.get("hidden", False),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }
    
    def update(
        self,
        label: str,
        value: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """Update a block's value or description.
        
        Args:
            label: Block label
            value: New value (if provided)
            description: New description (if provided)
            
        Returns:
            True if updated
        """
        block_path = self._block_path(label)
        if not block_path.exists():
            return False
        
        meta = self._load_meta(label)
        
        if meta.get("read_only") and value is not None:
            raise ValueError(f"Block '{label}' is read-only")
        
        limit = meta.get("limit", DEFAULT_LIMIT)
        if value is not None and len(value) > limit:
            raise ValueError(f"Value length ({len(value)}) exceeds limit ({limit})")
        
        # Update content
        if value is not None:
            block_path.write_text(value)
        
        # Update metadata
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        if description is not None:
            meta["description"] = description
        self._save_meta(label, meta)
        
        logger.debug(f"Updated block '{label}'")
        return True
    
    def delete(self, label: str) -> bool:
        """Delete a block.
        
        Args:
            label: Block label
            
        Returns:
            True if deleted
        """
        block_path = self._block_path(label)
        meta_path = self._meta_path(label)
        
        if not block_path.exists():
            return False
        
        block_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        
        logger.info(f"Deleted block '{label}'")
        return True
    
    def list_blocks(self, include_hidden: bool = False) -> list[dict]:
        """List all blocks.
        
        Args:
            include_hidden: Include hidden blocks
            
        Returns:
            List of blocks
        """
        blocks = []
        for block_path in self.blocks_dir.glob("*.md"):
            label = block_path.stem
            block = self.get_by_label(label)
            if block:
                if not include_hidden and block.get("hidden"):
                    continue
                blocks.append(block)
        
        # Order: project, tools, tasks, capabilities, human (human last = closest to messages)
        # Note: identity block is NOT in this list - it's used as system prompt, not in memory_blocks
        order = {"project": 0, "tools": 1, "tasks": 2, "capabilities": 3, "human": 4}
        return sorted(blocks, key=lambda b: (order.get(b["label"], 50), b["label"]))
    
    def str_replace(self, label: str, old_str: str, new_str: str) -> bool:
        """Replace text in a block.
        
        Args:
            label: Block label
            old_str: Text to find
            new_str: Replacement text
            
        Returns:
            True if replaced
        """
        block = self.get_by_label(label)
        if not block:
            raise ValueError(f"Block '{label}' not found")
        
        if old_str not in block["value"]:
            raise ValueError(f"Text '{old_str}' not found in block '{label}'")
        
        new_value = block["value"].replace(old_str, new_str, 1)
        return self.update(label, value=new_value)
    
    def append(self, label: str, text: str) -> bool:
        """Append text to a block.
        
        Args:
            label: Block label
            text: Text to append
            
        Returns:
            True if appended
        """
        block = self.get_by_label(label)
        if not block:
            raise ValueError(f"Block '{label}' not found")
        
        new_value = block["value"] + text
        return self.update(label, value=new_value)
