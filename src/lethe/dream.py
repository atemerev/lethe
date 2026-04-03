"""Dream processing — learn from conversations via filtered LoRA training.

Nightly pipeline:
1. Collect conversations from all users
2. Anonymize (strip PII)
3. Extract reasoning/knowledge/emotional patterns
4. Quality gate
5. Incremental LoRA training
6. Hot-reload adapter in vLLM

The entity literally grows smarter from conversations without
retaining who said what.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# PII anonymization patterns
ANONYMIZATION_PATTERNS = {
    "emails": (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    "phones": (re.compile(r"\b\+?\d[\d\s\-()]{7,}\b"), "[PHONE]"),
    "urls": (re.compile(r"https?://\S+"), "[URL]"),
    "dates": (re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"), "[DATE]"),
    "numeric_ids": (re.compile(r"\b\d{6,12}\b"), "[ID]"),
}


class DreamProcessor:
    """Nightly pipeline: anonymize conversations → extract patterns → train LoRA."""

    def __init__(
        self,
        workspace_dir: str,
        llm_complete=None,  # async callable for frontier LLM (pattern extraction)
        training_data_path: str = "",
    ):
        self._workspace_dir = workspace_dir
        self._llm_complete = llm_complete
        self._dream_dir = os.path.join(workspace_dir, "dream")
        self._training_data_path = training_data_path or os.path.join(self._dream_dir, "training_set.jsonl")
        os.makedirs(self._dream_dir, exist_ok=True)

    async def run_dream_cycle(self, conversations: list[dict]) -> dict:
        """Full dream cycle. Returns stats about what was processed.

        Args:
            conversations: List of conversation dicts, each with:
                - messages: list of {role, content} dicts
                - user_id: identifier (will be stripped)
                - timestamp: when conversation happened

        Returns:
            Stats dict: {collected, anonymized, examples_extracted, examples_accepted}
        """
        stats = {
            "collected": len(conversations),
            "anonymized": 0,
            "examples_extracted": 0,
            "examples_accepted": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not conversations:
            logger.info("Dream cycle: no conversations to process")
            return stats

        # Step 1: Anonymize
        anonymized = []
        for conv in conversations:
            anon = self.anonymize_conversation(conv)
            if anon:
                anonymized.append(anon)
        stats["anonymized"] = len(anonymized)

        if not anonymized:
            logger.info("Dream cycle: nothing survived anonymization")
            return stats

        # Step 2: Extract patterns
        examples = []
        for conv in anonymized:
            extracted = await self.extract_patterns(conv)
            examples.extend(extracted)
        stats["examples_extracted"] = len(examples)

        # Step 3: Quality gate
        accepted = self.quality_gate(examples)
        stats["examples_accepted"] = len(accepted)

        # Step 4: Append to training data
        if accepted:
            self._append_training_data(accepted)

        # Save dream log
        log_path = os.path.join(self._dream_dir, "dream_log.jsonl")
        try:
            with open(log_path, "a") as f:
                f.write(json.dumps(stats) + "\n")
        except Exception as e:
            logger.warning("Failed to write dream log: %s", e)

        logger.info(
            "Dream cycle complete: %d conversations → %d anonymized → %d examples → %d accepted",
            stats["collected"], stats["anonymized"],
            stats["examples_extracted"], stats["examples_accepted"],
        )
        return stats

    def anonymize_conversation(self, conversation: dict) -> Optional[dict]:
        """Strip PII from a conversation.

        Rule-based anonymization: emails, phones, URLs, dates, numeric IDs.
        Replaces with generic tokens.
        """
        messages = conversation.get("messages", [])
        if not messages:
            return None

        anonymized_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if not content:
                continue
            # Apply regex patterns
            for pattern_name, (pattern, replacement) in ANONYMIZATION_PATTERNS.items():
                content = pattern.sub(replacement, content)
            anonymized_messages.append({
                "role": msg.get("role", "user"),
                "content": content,
            })

        if not anonymized_messages:
            return None

        return {
            "messages": anonymized_messages,
            # user_id deliberately stripped — anonymized conversations have no owner
        }

    async def extract_patterns(self, conversation: dict) -> list[dict]:
        """Extract training examples from an anonymized conversation.

        Uses frontier LLM to identify useful patterns:
        - Reasoning chains
        - Domain knowledge
        - Emotional dynamics
        - Problem-solving approaches
        """
        if not self._llm_complete:
            # Without LLM, use raw conversation turns as examples
            return self._raw_turn_extraction(conversation)

        messages = conversation.get("messages", [])
        if len(messages) < 2:
            return []

        # Format conversation for LLM
        conv_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Entity'}: {m['content'][:500]}"
            for m in messages[:20]  # Limit for context
        )

        prompt = (
            "Extract training examples from this anonymized conversation.\n"
            "Focus on:\n"
            "- Interesting reasoning patterns\n"
            "- Domain knowledge worth learning\n"
            "- Emotional awareness and appropriate responses\n"
            "- Creative problem-solving approaches\n\n"
            "Skip:\n"
            "- Trivial greetings\n"
            "- Purely personal/identifying content\n"
            "- Repetitive patterns already well-known\n\n"
            f"CONVERSATION:\n{conv_text}\n\n"
            "Output a JSON array of training examples, each with:\n"
            '{"instruction": "...", "response": "...", "domain_tags": ["..."], "quality_score": 0.0-1.0}'
        )

        try:
            result = await self._llm_complete(prompt)
            # Try to parse JSON from the response
            examples = self._parse_json_array(result)
            return examples
        except Exception as e:
            logger.warning("Pattern extraction failed: %s", e)
            return self._raw_turn_extraction(conversation)

    def quality_gate(self, examples: list[dict]) -> list[dict]:
        """Filter examples by quality score and deduplication."""
        accepted = []
        seen_instructions = set()

        for ex in examples:
            # Require minimum quality score
            score = ex.get("quality_score", 0.0)
            if isinstance(score, (int, float)) and score < 0.5:
                continue

            # Basic deduplication
            instruction = ex.get("instruction", "")
            if not instruction or len(instruction) < 20:
                continue
            instruction_key = instruction[:100].lower().strip()
            if instruction_key in seen_instructions:
                continue
            seen_instructions.add(instruction_key)

            # Require a response
            response = ex.get("response", "")
            if not response or len(response) < 10:
                continue

            accepted.append(ex)

        return accepted

    def _raw_turn_extraction(self, conversation: dict) -> list[dict]:
        """Fallback: extract turn pairs as raw training examples."""
        messages = conversation.get("messages", [])
        examples = []
        for i in range(len(messages) - 1):
            if messages[i]["role"] == "user" and messages[i + 1]["role"] == "assistant":
                user_msg = messages[i]["content"]
                assistant_msg = messages[i + 1]["content"]
                if len(user_msg) > 20 and len(assistant_msg) > 20:
                    examples.append({
                        "instruction": user_msg[:1000],
                        "response": assistant_msg[:1000],
                        "domain_tags": [],
                        "quality_score": 0.6,
                    })
        return examples

    def _append_training_data(self, examples: list[dict]):
        """Append accepted examples to training data file."""
        try:
            os.makedirs(os.path.dirname(self._training_data_path), exist_ok=True)
            with open(self._training_data_path, "a") as f:
                for ex in examples:
                    # ShareGPT format for Unsloth/Axolotl
                    entry = {
                        "conversations": [
                            {"from": "human", "value": ex["instruction"]},
                            {"from": "gpt", "value": ex["response"]},
                        ],
                        "source": "dream_processing",
                        "quality_score": ex.get("quality_score", 0.6),
                        "domain_tags": ex.get("domain_tags", []),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    f.write(json.dumps(entry) + "\n")
            logger.info("Appended %d training examples to %s", len(examples), self._training_data_path)
        except Exception as e:
            logger.warning("Failed to append training data: %s", e)

    def _parse_json_array(self, text: str) -> list[dict]:
        """Try to parse a JSON array from LLM response text."""
        text = text.strip()
        # Try direct parse
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
        # Try finding array in text
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end > start:
            try:
                result = json.loads(text[start:end + 1])
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
        return []

    def get_training_stats(self) -> dict:
        """Get stats about accumulated training data."""
        count = 0
        try:
            if os.path.exists(self._training_data_path):
                with open(self._training_data_path, "r") as f:
                    count = sum(1 for _ in f)
        except Exception:
            pass
        return {"training_examples": count, "path": self._training_data_path}
