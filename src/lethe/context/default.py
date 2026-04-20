"""Default context assembler — baseline behavior for unrecognized models."""

from lethe.context import ContextAssembler, register


@register
class DefaultAssembler(ContextAssembler):
    """Fallback assembler. Embeds tool reference in prompt text."""

    model_patterns = []  # Never matched — used as explicit fallback

    def should_embed_tool_reference(self) -> bool:
        return True
