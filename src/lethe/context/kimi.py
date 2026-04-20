"""Kimi K2.5 context assembler.

Kimi needs tools visible in prompt text (not just tools parameter).
Uses walls-of-text guardrails in communication rules.
"""

from typing import Optional

from lethe.context import ContextAssembler, register


@register
class KimiAssembler(ContextAssembler):

    model_patterns = ["kimi"]

    def get_comm_rules_filename(self) -> Optional[str]:
        return "communication-kimi.md"

    def should_embed_tool_reference(self) -> bool:
        return True
