"""Model catalog loader — single source of truth in config/model_catalog.json."""

import json
import os
from pathlib import Path

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


def build_model_keyboard(provider: str, kind: str, current_model: str) -> list[list[dict]]:
    """Build inline keyboard button data for model selection.

    Returns list of rows, each row is a list of {text, callback_data} dicts.
    """
    catalog = MODEL_CATALOG.get(provider, {})
    models = catalog.get(kind, [])
    rows = []
    for name, model_id, pricing in models:
        marker = "\u2705 " if model_id == current_model else ""
        btn_text = f"{marker}{name} ({pricing})"
        callback_data = f"{kind}:{model_id}"
        if len(callback_data) > 64:
            callback_data = callback_data[:64]
        rows.append([{"text": btn_text, "callback_data": callback_data}])
    return rows
