"""Model catalog loader — reads from config/model_catalog.json (single source of truth)."""

import json
import os
from pathlib import Path

_CATALOG_PATHS = [
    Path(__file__).resolve().parent.parent / "config" / "model_catalog.json",  # dev: gateway/../config/
    Path(os.environ.get("WORKSPACE_DIR", os.path.expanduser("~/lethe"))) / "config" / "model_catalog.json",
]

PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}


def _load_catalog() -> dict:
    for p in _CATALOG_PATHS:
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


MODEL_CATALOG: dict = _load_catalog()
