"""Embedding engine — shared across all memory subsystems.

Uses ONNX Runtime directly with Snowflake/arctic-embed-m-v2.0 (int8).
Multilingual (76 languages), 768 dims, 297 MB model.
No PyTorch, no fastembed — just onnxruntime + tokenizers + huggingface_hub.
"""

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "Snowflake/snowflake-arctic-embed-m-v2.0"
EMBEDDING_DIM = 768
_ONNX_FILE = "onnx/model_int8.onnx"
_TOKENIZER_FILE = "tokenizer.json"
_MAX_LENGTH = 512

_MODEL_METADATA_FILE = "embedding_model.json"


class _Embedder:
    def __init__(self):
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        model_path = hf_hub_download(EMBEDDING_MODEL, _ONNX_FILE)
        tokenizer_path = hf_hub_download(EMBEDDING_MODEL, _TOKENIZER_FILE)

        self.session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(tokenizer_path)
        self.tokenizer.enable_truncation(max_length=_MAX_LENGTH)
        self.tokenizer.enable_padding(length=None)
        logger.info("Embedding model loaded")

    def embed(self, text: str, is_query: bool = True) -> list[float]:
        if is_query:
            text = f"query: {text}"
        encoded = self.tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
        outputs = self.session.run(
            ["sentence_embedding"],
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )
        vec = outputs[0][0]
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()


_instance: Optional[_Embedder] = None


def _get_embedder() -> _Embedder:
    global _instance
    if _instance is None:
        _instance = _Embedder()
    return _instance


def embed(text: str, is_query: bool = True) -> list[float]:
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM
    return _get_embedder().embed(text, is_query=is_query)


def needs_reindex(data_dir: Path) -> bool:
    meta_path = data_dir / _MODEL_METADATA_FILE
    if not meta_path.exists():
        return True
    try:
        stored = json.loads(meta_path.read_text())
        return (
            stored.get("model") != EMBEDDING_MODEL
            or stored.get("dim") != EMBEDDING_DIM
        )
    except Exception:
        return True


def save_model_metadata(data_dir: Path):
    meta_path = data_dir / _MODEL_METADATA_FILE
    meta_path.write_text(json.dumps({
        "model": EMBEDDING_MODEL,
        "dim": EMBEDDING_DIM,
    }))
