#!/usr/bin/env python3
"""LoRA finetuning loop for Lethe dream processing.

Reads training data accumulated by DreamProcessor (ShareGPT JSONL),
trains a LoRA adapter using Unsloth, and exports a merged GGUF model
for Ollama import.

Usage:
    # Train from accumulated dream data
    python scripts/train_lora.py

    # Custom paths
    python scripts/train_lora.py \
        --training-data workspace/dream/training_set.jsonl \
        --base-model Qwen/Qwen3-8B-Instruct \
        --output-dir outputs/lora \
        --export-gguf

    # Import into Ollama after training
    python scripts/train_lora.py --ollama-import lethe-dreamer

Hardware: Requires ~12GB VRAM for QLoRA (4-bit) on 8B models.
With 4x RTX 4090, training takes ~5-15 minutes per epoch.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Default paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_TRAINING_DATA = PROJECT_ROOT / "workspace" / "dream" / "training_set.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "lora"
DEFAULT_BASE_MODEL = "Qwen/Qwen3-8B-Instruct"

# Training hyperparameters
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
MAX_SEQ_LENGTH = 1024
LEARNING_RATE = 1e-4
NUM_EPOCHS = 3
BATCH_SIZE = 1
GRADIENT_ACCUMULATION = 8
WARMUP_STEPS = 50
SAVE_STEPS = 100


def load_training_data(path: Path) -> list[dict]:
    """Load ShareGPT-format training data from JSONL file."""
    examples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Validate ShareGPT format
                convs = entry.get("conversations", [])
                if len(convs) >= 2:
                    examples.append(entry)
            except json.JSONDecodeError:
                continue
    return examples


def format_for_training(examples: list[dict], tokenizer) -> list[dict]:
    """Convert ShareGPT examples to chat-formatted training strings."""
    formatted = []
    for ex in examples:
        messages = []
        for turn in ex["conversations"]:
            role = "user" if turn["from"] == "human" else "assistant"
            messages.append({"role": role, "content": turn["value"]})
        # Apply chat template
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False,
        )
        formatted.append({"text": text})
    return formatted


def train(args):
    """Run LoRA finetuning."""
    try:
        from unsloth import FastLanguageModel
    except ImportError:
        logger.error(
            "Unsloth not installed. Install with:\n"
            "  pip install unsloth\n"
            "Or for the full stack:\n"
            "  pip install 'unsloth[cu124-torch260]'"
        )
        sys.exit(1)

    import torch
    from datasets import Dataset
    from trl import SFTTrainer
    from transformers import TrainingArguments

    training_data_path = Path(args.training_data)
    if not training_data_path.exists():
        logger.error("Training data not found: %s", training_data_path)
        logger.info("Run the dream cycle first to accumulate training data.")
        sys.exit(1)

    # Load data
    examples = load_training_data(training_data_path)
    if len(examples) < 5:
        logger.error("Need at least 5 training examples, found %d", len(examples))
        sys.exit(1)
    logger.info("Loaded %d training examples from %s", len(examples), training_data_path)

    # Load base model
    logger.info("Loading base model: %s", args.base_model)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base_model,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype=None,  # auto-detect
        load_in_4bit=True,
    )

    # Attach LoRA adapter
    logger.info("Attaching LoRA adapter (r=%d, alpha=%d)", LORA_R, LORA_ALPHA)
    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=["q_proj", "v_proj"],
        bias="none",
        use_gradient_checkpointing="unsloth",
    )

    # Prepare dataset
    formatted = format_for_training(examples, tokenizer)
    dataset = Dataset.from_list(formatted)
    logger.info("Dataset prepared: %d examples", len(dataset))

    # Resume from checkpoint if available
    output_dir = Path(args.output_dir)
    resume_from = None
    if output_dir.exists():
        checkpoints = sorted(output_dir.glob("checkpoint-*"))
        if checkpoints:
            resume_from = str(checkpoints[-1])
            logger.info("Resuming from checkpoint: %s", resume_from)

    # Train
    training_args = TrainingArguments(
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION,
        warmup_steps=WARMUP_STEPS,
        num_train_epochs=NUM_EPOCHS,
        learning_rate=LEARNING_RATE,
        fp16=torch.cuda.is_available() and not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=10,
        output_dir=str(output_dir),
        save_strategy="steps",
        save_steps=SAVE_STEPS,
        save_total_limit=3,
        optim="adamw_8bit",
        seed=42,
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        packing=True,
        args=training_args,
    )

    logger.info("Starting training (%d epochs, batch=%d, grad_accum=%d)",
                NUM_EPOCHS, BATCH_SIZE, GRADIENT_ACCUMULATION)
    trainer.train(resume_from_checkpoint=resume_from)

    # Save final LoRA adapter
    adapter_path = output_dir / "adapter"
    model.save_pretrained(str(adapter_path))
    tokenizer.save_pretrained(str(adapter_path))
    logger.info("LoRA adapter saved to %s", adapter_path)

    # Import into Ollama if requested (uses Ollama's native adapter support)
    if args.ollama_import:
        ollama_import(adapter_path, args.ollama_import, args.base_model)

    # Legacy GGUF export (optional, for non-Ollama deployments)
    if args.export_gguf and not args.ollama_import:
        export_gguf(model, tokenizer, output_dir, args.gguf_quant)

    logger.info("Training complete.")
    return str(adapter_path)


def export_gguf(model, tokenizer, output_dir: Path, quant: str = "q4_k_m"):
    """Export merged model as GGUF for non-Ollama deployments."""
    gguf_dir = output_dir / "gguf"
    logger.info("Exporting merged GGUF (%s) to %s", quant, gguf_dir)
    model.save_pretrained_gguf(
        str(gguf_dir),
        tokenizer,
        quantization_method=quant,
    )
    logger.info("GGUF export complete: %s", gguf_dir)


def ollama_import(adapter_path: Path, model_name: str, base_model: str):
    """Import LoRA adapter into Ollama using native adapter support.

    Uses Ollama's Modelfile with FROM (base) + ADAPTER (LoRA safetensors).
    No GGUF conversion needed — Ollama handles quantization internally.
    """
    import subprocess

    # Map HuggingFace model name to Ollama model name
    ollama_base = os.environ.get("DREAM_OLLAMA_BASE", "qwen3.5:9b")

    # Create Ollama Modelfile
    modelfile_path = adapter_path.parent / "Modelfile"
    modelfile_content = f"""FROM {ollama_base}
ADAPTER {adapter_path}

PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER num_ctx {MAX_SEQ_LENGTH}

SYSTEM You are the subconscious layer of an autonomous AI entity. You handle pattern recognition, emotional salience, memory recall decisions, and reflective insight.
"""
    modelfile_path.write_text(modelfile_content)
    logger.info("Created Modelfile at %s (base: %s, adapter: %s)", modelfile_path, ollama_base, adapter_path)

    # Create model in Ollama
    logger.info("Importing into Ollama as '%s'...", model_name)
    result = subprocess.run(
        ["ollama", "create", model_name, "-f", str(modelfile_path)],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode == 0:
        logger.info("Successfully imported as ollama model '%s'", model_name)
    else:
        logger.error("Ollama import failed: %s", result.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="LoRA finetuning for Lethe dream processing",
    )
    parser.add_argument(
        "--training-data", type=str,
        default=str(DEFAULT_TRAINING_DATA),
        help="Path to training_set.jsonl from dream processor",
    )
    parser.add_argument(
        "--base-model", type=str,
        default=DEFAULT_BASE_MODEL,
        help="HuggingFace base model ID (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir", type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory for checkpoints and adapters",
    )
    parser.add_argument(
        "--export-gguf", action="store_true",
        help="Export merged model as GGUF after training",
    )
    parser.add_argument(
        "--gguf-quant", type=str, default="q4_k_m",
        choices=["q4_k_m", "q5_k_m", "q8_0", "f16"],
        help="GGUF quantization method (default: q4_k_m)",
    )
    parser.add_argument(
        "--ollama-import", type=str, default="",
        metavar="MODEL_NAME",
        help="Import into Ollama with this model name (implies --export-gguf)",
    )

    args = parser.parse_args()

    if args.ollama_import:
        args.export_gguf = True

    train(args)


if __name__ == "__main__":
    main()
