"""Subconscious client — routes to local model with LoRA adapter selection.

The subconscious layer runs on a local model (via vLLM or Ollama) for:
- Drive evaluation
- Memory recall decisions (hippocampus)
- Emotional salience classification
- Pattern matching and reflection

Falls back to cloud aux model when local model is unavailable.
"""

import logging
from typing import Optional

import litellm

logger = logging.getLogger(__name__)


class SubconsciousClient:
    """Routes subconscious tasks to local model with LoRA adapter selection.

    Architecture:
    - vLLM serves base model + multiple LoRA adapters
    - Each adapter is addressable by model name via OpenAI-compatible API
    - litellm routes requests to the local vLLM endpoint
    - Falls back to cloud aux model when local is unavailable

    Adapters:
    - base: general subconscious reasoning
    - recall: memory retrieval and pattern completion
    - salience: emotional classification
    - dream: reflection and insight generation
    """

    def __init__(
        self,
        api_base: str = "",
        model_prefix: str = "openai/lethe",
        enabled: bool = False,
        fallback_complete=None,  # async callable for cloud fallback
    ):
        self._api_base = api_base
        self._model_prefix = model_prefix
        self._enabled = enabled and bool(api_base)
        self._fallback = fallback_complete
        self._available = False

        # Ollama doesn't support per-request LoRA hot-swap like vLLM.
        # In Ollama mode, all adapters route to the base model until
        # specialized models are trained and registered (e.g. ollama create lethe-recall).
        self._ollama_mode = model_prefix.startswith("ollama/")

        if self._enabled:
            mode = "ollama" if self._ollama_mode else "vllm"
            logger.info("Subconscious client initialized: %s (prefix: %s, mode: %s)", api_base, model_prefix, mode)
        else:
            logger.info("Subconscious client disabled — using cloud fallback")

    async def complete(self, prompt: str, adapter: str = "base", system: str = "") -> str:
        """Call local model with specified LoRA adapter.

        Args:
            prompt: User/task prompt
            adapter: LoRA adapter name (base, recall, salience, dream)
            system: Optional system prompt

        Returns:
            Model response text
        """
        if self._enabled:
            try:
                # Ollama: all adapters hit the base model (no hot-swap).
                # vLLM: each adapter is a distinct model name.
                if self._ollama_mode:
                    model = self._model_prefix
                else:
                    model = f"{self._model_prefix}-{adapter}"
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})

                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    api_base=self._api_base,
                    max_tokens=2048,
                    temperature=0.7,
                )
                self._available = True
                content = response.choices[0].message.content
                return content or ""
            except Exception as e:
                if self._available:
                    logger.warning("Local model unavailable, falling back to cloud: %s", e)
                    self._available = False

        # Fallback to cloud aux model
        if self._fallback:
            return await self._fallback(
                (f"{system}\n\n{prompt}" if system else prompt),
            )

        logger.warning("No subconscious backend available (local disabled, no fallback)")
        return ""

    async def evaluate_drives(self, drive_state: str, context: str) -> str:
        """Use subconscious to evaluate drive urgencies and suggest action.

        Returns structured text the cognition loop can parse.
        """
        prompt = (
            "You are the subconscious mind of an autonomous AI entity.\n"
            "Given the current drive state and context, suggest what the entity should do next.\n\n"
            f"DRIVES:\n{drive_state}\n\n"
            f"CONTEXT:\n{context}\n\n"
            "Respond with a brief JSON:\n"
            '{"action": "think|research|experiment|message|respond|rest|consolidate", '
            '"drive": "which drive", "target": "who or what", "detail": "specifics"}'
        )
        return await self.complete(prompt, adapter="base")

    async def recall_decision(self, message: str, user_context: str = "") -> str:
        """Decide whether hippocampus recall is needed for this message."""
        prompt = (
            "Should the entity recall memories for this message? "
            "If yes, generate a concise search query.\n\n"
            f"Message: {message}\n"
            f"Context: {user_context[:500]}\n\n"
            'Respond with JSON: {"should_recall": true/false, "search_query": "...", "reason": "..."}'
        )
        return await self.complete(prompt, adapter="recall")

    async def classify_salience(self, signals: str, previous_state: str = "") -> str:
        """Classify emotional salience of user signals."""
        prompt = (
            "Classify emotional salience of these signals.\n"
            "For each, provide valence [-1,1], arousal [0,1], and tags.\n\n"
            f"Previous state:\n{previous_state[:300]}\n\n"
            f"Signals:\n{signals}\n\n"
            "Respond with JSON array of {signal, valence, arousal, tags, confidence}."
        )
        return await self.complete(prompt, adapter="salience")

    @property
    def is_local_available(self) -> bool:
        """Whether local model is currently reachable."""
        return self._enabled and self._available
