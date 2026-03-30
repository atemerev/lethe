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
# Order matters: OAuth (subscription) listed first = preferred when available
_PROVIDER_AUTH = {
    "openrouter": [
        ("OPENROUTER_API_KEY", "API"),
    ],
    "anthropic": [
        ("ANTHROPIC_AUTH_TOKEN", "sub"),  # Subscription (Max/Pro) — preferred
        ("ANTHROPIC_API_KEY", "API"),
    ],
    "openai": [
        ("OPENAI_AUTH_TOKEN", "sub"),  # Subscription (Plus/Pro) — preferred
        ("OPENAI_API_KEY", "API"),
    ],
}

# Base display labels
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}

# Auth type → display suffix
_AUTH_LABELS = {
    "sub": "subscription",
    "API": "API key",
}


def get_available_providers() -> list[dict]:
    """Return list of available providers with auth info.

    If a provider has both subscription and API key, both appear as separate
    entries so the user can choose. Subscription is listed first (preferred).

    Each entry: {"provider": "anthropic", "label": "Anthropic (subscription)", "auth": "sub"}
    """
    available = []
    for provider, auth_options in _PROVIDER_AUTH.items():
        if provider not in MODEL_CATALOG:
            continue
        for env_var, auth_type in auth_options:
            if os.environ.get(env_var):
                base_label = _PROVIDER_LABELS.get(provider, provider)
                suffix = _AUTH_LABELS.get(auth_type, auth_type)
                if provider == "openrouter":
                    label = base_label  # No suffix for OpenRouter
                else:
                    label = f"{base_label} ({suffix})"
                available.append({
                    "provider": provider,
                    "label": label,
                    "auth": auth_type,
                })
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
