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

# Provider auth detection: (env_var, auth_type_label)
_PROVIDER_AUTH = {
    "openrouter": [
        ("OPENROUTER_API_KEY", "API"),
    ],
    "anthropic": [
        ("ANTHROPIC_API_KEY", "API"),
        ("ANTHROPIC_AUTH_TOKEN", "OAuth"),
    ],
    "openai": [
        ("OPENAI_API_KEY", "API"),
        ("OPENAI_AUTH_TOKEN", "OAuth"),
    ],
}

# Base display labels
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}


def get_available_providers() -> list[dict]:
    """Return list of available providers with auth info.

    Each entry: {"provider": "anthropic", "label": "Anthropic (OAuth)", "auth": "OAuth"}
    """
    available = []
    for provider, auth_options in _PROVIDER_AUTH.items():
        if provider not in MODEL_CATALOG:
            continue
        for env_var, auth_type in auth_options:
            if os.environ.get(env_var):
                base_label = _PROVIDER_LABELS.get(provider, provider)
                label = f"{base_label} ({auth_type})" if auth_type != "API" or provider != "openrouter" else base_label
                available.append({
                    "provider": provider,
                    "label": label,
                    "auth": auth_type,
                })
                break  # First match wins (API key takes precedence over OAuth)
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
