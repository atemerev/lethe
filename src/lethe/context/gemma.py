"""Gemma 4 context assembler.

Gemma follows system prompts faithfully — minimal intervention needed.
Needs tool reference in prompt text for reliable tool calling.
"""

from typing import Optional

from lethe.context import ContextAssembler, register


@register
class GemmaAssembler(ContextAssembler):

    model_patterns = ["gemma"]

    def get_comm_rules_filename(self) -> Optional[str]:
        return "communication-gemma.md"

    def should_embed_tool_reference(self) -> bool:
        return True
