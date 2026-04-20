"""Claude (Anthropic) context assembler.

Claude has strong RLHF-trained personality priors that resist persona overrides.
This assembler counteracts that with:
- Persona reinforcement loaded from config/prompts/claude_persona.md
- Longer cache TTL on identity block (rarely changes)
- No tool reference in prompt text (Claude uses tools API natively)
"""

from typing import Dict, List, Optional

from lethe.context import ContextAssembler, SystemComponents, _render_block, register
from lethe.prompts import load_prompt_template


@register
class ClaudeAssembler(ContextAssembler):

    model_patterns = ["claude", "anthropic"]

    def build_system_prompt(self, components: SystemComponents) -> str:
        """Inject persona reinforcement between identity and instructions."""
        persona = load_prompt_template("claude_persona")

        parts = []
        if components.identity:
            parts.append(components.identity)
        if persona:
            parts.append(persona)
        if components.instructions:
            parts.append(components.instructions)
        if components.tools_doc:
            parts.append(components.tools_doc)
        if components.comm_rules:
            parts.append(components.comm_rules)
        return "\n\n".join(parts)

    def get_comm_rules_filename(self) -> Optional[str]:
        return "communication-anthropic.md"

    def build_system_blocks(
        self,
        *,
        system_prompt: str,
        memory_context: str,
        summary: str,
        transient_context: str,
        tool_reference: str,
    ) -> List[Dict]:
        """Claude-specific block assembly with longer cache TTL on identity."""
        blocks = []

        identity_block = _render_block("identity_block", system_prompt)
        blocks.append({
            "type": "text",
            "text": identity_block,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        })

        if memory_context:
            blocks.append({
                "type": "text",
                "text": _render_block("memory_context_block", memory_context),
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
        return False
