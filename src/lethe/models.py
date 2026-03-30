"""Model catalog loader — single source of truth in config/model_catalog.json."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve catalog path relative to project root (works for both direct and installed)
_CATALOG_PATHS = [
    Path(__file__).resolve().parent.parent.parent / "config" / "model_catalog.json",  # dev: src/lethe/../../config/
    Path(os.environ.get("WORKSPACE_DIR", os.path.expanduser("~/lethe"))) / "config" / "model_catalog.json",
]


def _load_catalog() -> dict:
    for p in _CATALOG_PATHS:
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            # Strip metadata keys
            return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


MODEL_CATALOG: dict = _load_catalog()

# Provider → env key (or auth token fallback) used to detect availability
_PROVIDER_KEYS = {
    "openrouter": ["OPENROUTER_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
    "openai": ["OPENAI_API_KEY", "OPENAI_AUTH_TOKEN"],
}

# Short display labels for provider headers
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}


def get_available_providers() -> list[str]:
    """Return list of providers that have API keys configured."""
    available = []
    for provider, keys in _PROVIDER_KEYS.items():
        if provider not in MODEL_CATALOG:
            continue
        if any(os.environ.get(k) for k in keys):
            available.append(provider)
    return available


def provider_for_model(model_id: str) -> str | None:
    """Detect which provider a model_id belongs to based on catalog lookup."""
    for provider, kinds in MODEL_CATALOG.items():
        for kind_models in kinds.values():
            for _name, mid, _price in kind_models:
                if mid == model_id:
                    return provider
    # Fallback heuristics
    if model_id.startswith("openrouter/"):
        return "openrouter"
    if "claude" in model_id.lower():
        return "anthropic"
    if "gpt" in model_id.lower():
        return "openai"
    return None
