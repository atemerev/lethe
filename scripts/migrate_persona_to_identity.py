#!/usr/bin/env python3
"""Migrate persona.md (first person) to identity.md (second person).

Uses LLM to convert first person ("I am", "I'm") to second person ("You are", "You're").
Reads LLM config from .env file.

Usage:
    uv run python scripts/migrate_persona_to_identity.py
    
    # Or with custom paths:
    uv run python scripts/migrate_persona_to_identity.py --memory-dir /path/to/memory
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv()

import litellm

CONVERSION_PROMPT = """Convert the following persona description from first person to second person.

Rules:
- "I am" → "You are"
- "I'm" → "You are" or "You're"  
- "my" → "your"
- "me" → "you"
- "I" → "You"
- Keep all other content exactly the same
- Preserve all formatting, XML tags, markdown, etc.
- Do NOT add any commentary or explanation
- Output ONLY the converted text

Input (first person):
{input_text}

Output (second person):"""


def get_llm_model() -> str:
    """Get LLM model from environment."""
    # Try various env vars
    model = os.environ.get("LLM_MODEL") or os.environ.get("LLM_MODEL_AUX")
    
    if not model:
        # Default fallback
        if os.environ.get("OPENROUTER_API_KEY"):
            model = "openrouter/google/gemini-2.0-flash-001"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            model = "claude-3-haiku-20240307"
        elif os.environ.get("OPENAI_API_KEY"):
            model = "gpt-4o-mini"
        else:
            print("Error: No LLM API key found in environment")
            print("Set one of: OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY")
            sys.exit(1)
    
    return model


def convert_to_second_person(text: str, model: str) -> str:
    """Use LLM to convert first person to second person."""
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "user", "content": CONVERSION_PROMPT.format(input_text=text)}
        ],
        temperature=0.3,
        max_tokens=8000,
    )
    return response.choices[0].message.content.strip()


def migrate(memory_dir: Path, dry_run: bool = False) -> bool:
    """Migrate persona.md to identity.md.
    
    Returns True if migration was performed.
    """
    persona_path = memory_dir / "persona.md"
    persona_meta_path = memory_dir / "persona.meta.json"
    identity_path = memory_dir / "identity.md"
    identity_meta_path = memory_dir / "identity.meta.json"
    capabilities_path = memory_dir / "capabilities.md"
    capabilities_meta_path = memory_dir / "capabilities.meta.json"
    
    # Check if migration needed
    if identity_path.exists():
        print(f"✓ identity.md already exists at {identity_path}")
        return False
    
    if not persona_path.exists():
        print(f"✗ No persona.md found at {persona_path}")
        return False
    
    print(f"Found persona.md at {persona_path}")
    
    # Read persona content
    persona_content = persona_path.read_text()
    print(f"  - {len(persona_content)} characters")
    
    if dry_run:
        print("\n[DRY RUN] Would convert to second person and create:")
        print(f"  - {identity_path}")
        print(f"  - {identity_meta_path}")
        return False
    
    # Convert to second person using LLM
    model = get_llm_model()
    print(f"\nConverting to second person using {model}...")
    
    identity_content = convert_to_second_person(persona_content, model)
    print(f"  - Converted: {len(identity_content)} characters")
    
    # Write identity.md
    identity_path.write_text(identity_content)
    print(f"✓ Created {identity_path}")
    
    # Create identity.meta.json
    identity_meta = {
        "label": "identity",
        "description": "System prompt - who you are, biography, communication style, output format. Written in second person.",
        "limit": 20000
    }
    identity_meta_path.write_text(json.dumps(identity_meta, indent=2))
    print(f"✓ Created {identity_meta_path}")
    
    # Create capabilities.md if it doesn't exist
    if not capabilities_path.exists():
        capabilities_content = """# Capabilities

## System Access

You have full access to your principal's machine:
- Filesystem - read, write, modify any files
- Command line - run any bash commands, scripts
- Browser - browse the web, automate interactions
- Codebases - read, edit, commit code
- Any installed tools - if something's not installed, you figure it out

## Work Style

You work asynchronously and thoroughly. When given a task, you actually do it - not just explain how. For multi-step tasks, you keep your principal informed of progress but don't wait for permission unless something's risky.

## Memory System

You maintain memory blocks about:
- **identity** - who you are (used as system prompt)
- **human** - what you learn about your principal over time
- **project** - current work context
- **capabilities** - this block

Plus long-term archival memory you can search. Your history together matters to you.
"""
        capabilities_path.write_text(capabilities_content)
        print(f"✓ Created {capabilities_path}")
        
        capabilities_meta = {
            "label": "capabilities",
            "description": "What you can do - system access, tools, work style, memory system.",
            "limit": 20000
        }
        capabilities_meta_path.write_text(json.dumps(capabilities_meta, indent=2))
        print(f"✓ Created {capabilities_meta_path}")
    
    # Optionally remove old persona files
    print(f"\n⚠ Old persona files kept for backup:")
    print(f"  - {persona_path}")
    if persona_meta_path.exists():
        print(f"  - {persona_meta_path}")
    print(f"\nTo complete migration, manually delete them after verifying identity.md is correct.")
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate persona.md to identity.md (first → second person)")
    parser.add_argument("--memory-dir", type=Path, default=None,
                        help="Path to memory directory (default: workspace/memory)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    args = parser.parse_args()
    
    # Find memory directory
    if args.memory_dir:
        memory_dir = args.memory_dir
    else:
        # Try common locations
        candidates = [
            Path("workspace/memory"),
            Path(os.environ.get("MEMORY_DIR", "")) if os.environ.get("MEMORY_DIR") else None,
            Path.home() / "lethe" / "data" / "memory",
        ]
        memory_dir = None
        for candidate in candidates:
            if candidate and candidate.exists():
                memory_dir = candidate
                break
        
        if not memory_dir:
            print("Error: Could not find memory directory")
            print("Specify with --memory-dir or set MEMORY_DIR env var")
            sys.exit(1)
    
    print(f"Memory directory: {memory_dir}")
    print()
    
    if migrate(memory_dir, dry_run=args.dry_run):
        print("\n✓ Migration complete!")
        print("\nRestart Lethe to use the new identity block:")
        print("  systemctl --user restart lethe")
    else:
        print("\nNo migration needed.")


if __name__ == "__main__":
    main()
