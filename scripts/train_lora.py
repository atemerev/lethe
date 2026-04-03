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

    # Import into Ollama: merge LoRA → convert to GGUF → quantize → ollama create
    if args.ollama_import:
        ollama_import(adapter_path, args.ollama_import, args.base_model,
                      output_dir, args.gguf_quant)

    logger.info("Training complete.")
    return str(adapter_path)


def ollama_import(adapter_path: Path, model_name: str, base_model: str,
                  output_dir: Path, quant: str = "q4_k_m"):
    """Merge LoRA into base model, convert to GGUF, quantize, import into Ollama.

    Pipeline: PEFT merge (CPU) → llama.cpp convert_hf_to_gguf → llama-quantize → ollama create
    """
    import subprocess as sp
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel
    import torch

    llama_cpp_dir = os.environ.get("LLAMA_CPP_DIR", os.path.expanduser("~/devel/llama.cpp"))
    convert_script = os.path.join(llama_cpp_dir, "convert_hf_to_gguf.py")
    quantize_bin = os.path.join(llama_cpp_dir, "build", "bin", "llama-quantize")

    if not os.path.exists(convert_script):
        logger.error("llama.cpp not found at %s. Set LLAMA_CPP_DIR env var.", llama_cpp_dir)
        return
    if not os.path.exists(quantize_bin):
        logger.error("llama-quantize not found. Build llama.cpp first.")
        return

    merged_dir = output_dir / "merged"
    f16_gguf = output_dir / f"{model_name}-f16.gguf"
    quant_gguf = output_dir / f"{model_name}-{quant}.gguf"

    # Step 1: Merge LoRA into base model on CPU
    logger.info("Merging LoRA adapter into base model (CPU)...")
    base = AutoModelForCausalLM.from_pretrained(
        base_model, torch_dtype=torch.bfloat16, device_map="cpu",
    )
    model = PeftModel.from_pretrained(base, str(adapter_path))
    merged = model.merge_and_unload()
    merged.save_pretrained(str(merged_dir), safe_serialization=True)
    tokenizer = AutoTokenizer.from_pretrained(str(adapter_path))
    tokenizer.save_pretrained(str(merged_dir))
    del base, model, merged
    logger.info("Merged model saved to %s", merged_dir)

    # Step 2: Convert to f16 GGUF
    logger.info("Converting to f16 GGUF...")
    result = sp.run(
        [sys.executable, convert_script, str(merged_dir),
         "--outfile", str(f16_gguf), "--outtype", "f16"],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.error("GGUF conversion failed: %s", result.stderr[-500:])
        return

    # Step 3: Quantize
    logger.info("Quantizing to %s...", quant)
    result = sp.run(
        [quantize_bin, str(f16_gguf), str(quant_gguf), quant],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.error("Quantization failed: %s", result.stderr[-500:])
        return

    # Step 4: Create Ollama model
    modelfile_path = output_dir / "Modelfile"
    modelfile_path.write_text(
        f"FROM {quant_gguf}\n\n"
        f"PARAMETER temperature 0.7\nPARAMETER top_p 0.9\n"
        f"PARAMETER num_ctx {MAX_SEQ_LENGTH}\n\n"
        "SYSTEM You are the subconscious layer of an autonomous AI entity. "
        "You handle pattern recognition, emotional salience, memory recall "
        "decisions, and reflective insight.\n"
    )
    logger.info("Importing into Ollama as '%s'...", model_name)
    result = sp.run(
        ["ollama", "create", model_name, "-f", str(modelfile_path)],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode == 0:
        logger.info("Successfully imported as ollama model '%s'", model_name)
    else:
        logger.error("Ollama import failed: %s", result.stderr)

    # Cleanup intermediate files
    import shutil
    for p in [merged_dir, f16_gguf]:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)
    logger.info("Cleaned up intermediate files")


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
