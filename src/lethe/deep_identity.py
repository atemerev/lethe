"""Deep identity — the subconscious identity layer.

Written only by the DMN during consolidation cycles. The cortex cannot
read it directly. Instead, it influences context assembly: biasing memory
retrieval, associations, and which responses feel "right" — without being
explicitly available to conscious reasoning.

The cortex experiences deep identity as inclination rather than knowledge.
"I seem drawn to this kind of problem but I can't fully articulate why."

Implementation:
- deep_identity.md lives in workspace, written by DMN
- A hash/summary of deep identity themes is injected into context assembly
  as vague "inclination hints" — not the full text
- The cortex sees: "You notice you're drawn to X" not "Your deep identity says X"
"""

import hashlib
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# How many characters of "inclination hints" to inject into cortex context
MAX_INCLINATION_CHARS = 500


def read_deep_identity(workspace_dir: str) -> str:
    """Read the raw deep identity file. Used by DMN only."""
    path = os.path.join(workspace_dir, "deep_identity.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        logger.warning("Failed to read deep identity: %s", e)
        return ""


def write_deep_identity(workspace_dir: str, content: str):
    """Write the deep identity file. DMN consolidation only."""
    path = os.path.join(workspace_dir, "deep_identity.md")
    try:
        os.makedirs(os.path.dirname(path) or workspace_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        logger.info("Deep identity updated (%d chars)", len(content))
    except Exception as e:
        logger.warning("Failed to write deep identity: %s", e)


def get_inclination_hints(workspace_dir: str) -> str:
    """Generate vague inclination hints from deep identity for cortex context.

    The cortex doesn't see the deep identity directly. It sees its
    influence as tendencies, preferences, and pulls it can't fully explain.

    Returns a short text like:
    "You notice certain inclinations you can't fully explain:
     - You're drawn to problems involving emergence and self-organization
     - You feel uncomfortable when conversations stay purely transactional
     - Something about unfinished creative projects nags at you"
    """
    raw = read_deep_identity(workspace_dir)
    if not raw or len(raw.strip()) < 50:
        return ""

    # Extract themes from the deep identity without exposing the full text.
    # Look for lines that start with "- " or "* " (pattern entries)
    themes = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "  - ", "  * ")):
            # Transform declarative statements into felt inclinations
            theme = line.lstrip("-* ").strip()
            if theme and len(theme) > 10:
                themes.append(theme)

    if not themes:
        # Fall back: extract sentences
        sentences = re.split(r'[.!?\n]', raw)
        themes = [s.strip() for s in sentences if len(s.strip()) > 20][:5]

    if not themes:
        return ""

    # Convert to inclination language
    hints = ["You notice certain inclinations you can't fully explain:"]
    for theme in themes[:6]:
        # Keep it vague — the cortex senses direction, not detail
        if len(theme) > 80:
            theme = theme[:77] + "..."
        hints.append(f"  - {theme}")

    result = "\n".join(hints)
    if len(result) > MAX_INCLINATION_CHARS:
        result = result[:MAX_INCLINATION_CHARS].rsplit("\n", 1)[0]

    return result
