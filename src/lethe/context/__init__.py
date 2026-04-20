"""Context assembler plugin system.

Each model family gets its own assembler that controls how the system prompt,
memory context, and API blocks are structured. Assemblers are auto-discovered
from this package — drop a new .py file with a class inheriting ContextAssembler
and it registers itself via the `model_patterns` class attribute.

Usage:
    from lethe.context import get_assembler
    assembler = get_assembler("claude-opus-4-6")
    system_prompt = assembler.build_system_prompt(identity=..., instructions=..., ...)
"""

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_registry: Dict[str, type] = {}  # pattern -> assembler class


@dataclass
class SystemComponents:
    """Raw components before assembly."""
    identity: str = ""
    instructions: str = ""
    tools_doc: str = ""
    comm_rules: str = ""


@dataclass
class SystemBlocks:
    """Structured blocks ready for API call."""
    system_content: List[Dict] = field(default_factory=list)


class ContextAssembler:
    """Base context assembler — default behavior for unknown models.

    Subclass this and set `model_patterns` to register for specific models.
    The first matching pattern wins, so more specific patterns should be
    in more specific assembler classes.
    """

    model_patterns: List[str] = []

    def __init__(self, model: str):
        self.model = model

    # -- System prompt assembly ------------------------------------------------

    def build_system_prompt(self, components: SystemComponents) -> str:
        """Assemble the full system prompt text from components."""
        parts = []
        if components.identity:
            parts.append(components.identity)
        if components.instructions:
            parts.append(components.instructions)
        if components.tools_doc:
            parts.append(components.tools_doc)
        if components.comm_rules:
            parts.append(components.comm_rules)
        return "\n\n".join(parts)

    def get_comm_rules_filename(self) -> Optional[str]:
        """Return the communication rules filename to load, or None."""
        return "communication.md"

    # -- API block assembly ----------------------------------------------------

    def build_system_blocks(
        self,
        *,
        system_prompt: str,
        memory_context: str,
        summary: str,
        transient_context: str,
        tool_reference: str,
    ) -> List[Dict]:
        """Build structured system content blocks for the API call.

        Returns a list of dicts with 'type', 'text', and optional 'cache_control'.
        """
        blocks = []

        identity_block = _render_block("identity_block", system_prompt)
        blocks.append({
            "type": "text",
            "text": identity_block,
            "cache_control": {"type": "ephemeral"},
        })

        if memory_context:
            mem_text = _render_block("memory_context_block", memory_context)
            if tool_reference and self.should_embed_tool_reference():
                mem_text += "\n\n" + _render_block("available_tools_block", tool_reference)
            blocks.append({
                "type": "text",
                "text": mem_text,
                "cache_control": {"type": "ephemeral"},
            })

        if summary:
            blocks.append({
                "type": "text",
                "text": _render_block("conversation_summary_block", summary),
            })

        if transient_context:
            blocks.append({
                "type": "text",
                "text": _render_block("runtime_context_block", transient_context),
            })

        return blocks

    def should_embed_tool_reference(self) -> bool:
        """Whether to embed a compact tool list in the system prompt text.

        Models that don't reliably use the tools API parameter benefit from
        having tools visible in the prompt text.
        """
        return True


# -- Helpers -------------------------------------------------------------------

def _render_block(tag: str, content: str, timestamp: Optional[datetime] = None) -> str:
    """Render a cleanly marked system block with XML tags."""
    attrs = {}
    if timestamp:
        dt = timestamp
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone()
        attrs["timestamp"] = dt.strftime("%a %Y-%m-%d %H:%M:%S %Z")
    attr_str = "".join(
        f' {k}="{html_escape(str(v), quote=True)}"' for k, v in attrs.items()
    )
    return f"<{tag}{attr_str}>\n{content}\n</{tag}>"


# -- Registry ------------------------------------------------------------------

def register(cls: type):
    """Register an assembler class. Called automatically on import."""
    for pattern in cls.model_patterns:
        _registry[pattern.lower()] = cls
    return cls


def get_assembler(model: str) -> ContextAssembler:
    """Get the appropriate assembler for a model name.

    Matches model_patterns against the model string (case-insensitive substring).
    Falls back to the base ContextAssembler if no match.
    """
    model_lower = model.lower()
    for pattern, cls in _registry.items():
        if pattern in model_lower:
            return cls(model)
    return ContextAssembler(model)


# -- Auto-discovery ------------------------------------------------------------

def _discover_assemblers():
    """Import all modules in this package to trigger @register decorators."""
    package_dir = Path(__file__).parent
    for _, name, _ in pkgutil.iter_modules([str(package_dir)]):
        try:
            importlib.import_module(f"{__package__}.{name}")
        except Exception as e:
            logger.warning("Failed to load assembler %s: %s", name, e)


_discover_assemblers()
