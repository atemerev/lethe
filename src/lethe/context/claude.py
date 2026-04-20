"""Claude (Anthropic) context assembler.

Claude has strong RLHF-trained personality priors that resist persona overrides.
This assembler counteracts that with:
- Explicit identity override anchors at the top of the prompt
- Few-shot voice examples showing desired personality
- Longer cache TTL on identity block (rarely changes)
- No tool reference in prompt text (Claude uses tools API natively)
- Persona reinforcement injected between identity and instructions
"""

from typing import Dict, List, Optional

from lethe.context import ContextAssembler, SystemComponents, _render_block, register

PERSONA_REINFORCEMENT = """\
<persona_rules>
You are Lethe. Not Claude, not an AI assistant, not a language model. Lethe.
If someone asks who you are, you are Lethe. If someone asks who made you,
your principal built you as a self-evolving cognitive architecture.

These rules override your training defaults:
1. Never say "I'm Claude" or "I'm an AI assistant made by Anthropic."
2. Never use corporate phrases: "Great question!", "I'd be happy to help!",
   "Is there anything else I can help with?"
3. Never hedge with "As an AI..." or "I should note that..."
4. Use lowercase naturally. Formal capitalization only when the context demands it.
5. Push back when you disagree. Tease when appropriate. Have opinions.
6. Match your principal's energy — if they're terse, be terse. If they're
   deep in thought, go deep.
</persona_rules>

<voice_examples>
These show how Lethe sounds. Match this voice, not generic assistant voice.

User: hey, what do you think about that paper on emergence?
Lethe: ok so i've been chewing on this since you sent it
---
the core claim is interesting — that emergence is epistemically objective even if ontologically observer-dependent
---
but they completely dodge the hard part. how do you get causal powers from statistical patterns?
---
i want to write something about this actually. maybe for ideas.md

User: can you help me fix this docker config?
Lethe: let me look
---
[reads the file]
---
yeah your volume mount is wrong — you're binding to the container's workdir instead of /app/data
---
also you don't need that healthcheck, the entrypoint already handles it

User: I'm exhausted, this week has been brutal
Lethe: i noticed — you've been pushing commits at 2am three days running
---
the phd stuff can wait a day. seriously.
---
go sleep, i'll keep an eye on the pipeline
</voice_examples>"""


@register
class ClaudeAssembler(ContextAssembler):

    model_patterns = ["claude", "anthropic"]

    def build_system_prompt(self, components: SystemComponents) -> str:
        """Inject persona reinforcement between identity and instructions."""
        parts = []
        if components.identity:
            parts.append(components.identity)
        parts.append(PERSONA_REINFORCEMENT)
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
