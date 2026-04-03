"""Memory fading — irrelevant memories decay unless emotionally tagged or recalled.

Inspired by biological memory: hippocampal memories consolidate into
long-term storage when they're emotionally significant or repeatedly recalled.
Memories that aren't tagged or accessed gradually fade — their retrieval
score is penalized by age, making them less likely to surface.

This creates natural forgetting: the entity doesn't remember every detail
of every conversation. What persists is what mattered.

Mechanisms:
1. **Age decay**: Older memories get lower retrieval scores unless refreshed
2. **Emotional anchoring**: Salience-tagged memories resist decay
3. **Recall reinforcement**: Every time a memory is recalled, its freshness resets
4. **Consolidation**: Memories that survive long enough with high salience
   get moved to archival (permanent) storage by the DMN
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Decay half-life in hours — after this many hours, an untagged memory's
# score is halved. Emotionally tagged memories decay much slower.
UNTAGGED_HALF_LIFE_HOURS = 72.0       # 3 days
TAGGED_HALF_LIFE_HOURS = 720.0        # 30 days
RECALLED_BOOST_HOURS = 48.0           # Recalling resets freshness by this much

# Minimum age factor — even very old memories don't go to zero
MIN_AGE_FACTOR = 0.05


def compute_age_factor(
    created_at: float,
    last_recalled_at: Optional[float] = None,
    salience_score: float = 0.0,
    now: Optional[float] = None,
) -> float:
    """Compute an age-based decay factor for a memory.

    Args:
        created_at: Unix timestamp when memory was created
        last_recalled_at: Unix timestamp of most recent recall (None if never recalled)
        salience_score: Emotional salience [0, 1] — higher = slower decay
        now: Current time (defaults to time.time())

    Returns:
        Factor in [MIN_AGE_FACTOR, 1.0] — multiply with retrieval score
    """
    if now is None:
        now = time.time()

    # Use the most recent "refresh" time — creation or last recall
    reference_time = created_at
    if last_recalled_at and last_recalled_at > created_at:
        reference_time = last_recalled_at

    age_hours = max(0, (now - reference_time)) / 3600.0

    # Choose half-life based on salience
    # Salience 0 → untagged half-life (72h)
    # Salience 1 → tagged half-life (720h)
    # Linear interpolation between them
    half_life = UNTAGGED_HALF_LIFE_HOURS + salience_score * (TAGGED_HALF_LIFE_HOURS - UNTAGGED_HALF_LIFE_HOURS)

    # Exponential decay: factor = 2^(-age/half_life)
    factor = math.pow(2, -age_hours / half_life)

    return max(MIN_AGE_FACTOR, factor)


def apply_fading_to_results(
    results: list[dict],
    now: Optional[float] = None,
) -> list[dict]:
    """Apply memory fading to a list of search results.

    Each result dict should have:
    - _distance or score: original retrieval score
    - created_at: timestamp (float or ISO string)
    - last_recalled_at: timestamp or None
    - salience_score: float 0-1

    Returns results with adjusted scores, sorted by faded score descending.
    """
    if now is None:
        now = time.time()

    faded = []
    for result in results:
        # Parse timestamps
        created = _parse_timestamp(result.get("created_at", 0))
        recalled = _parse_timestamp(result.get("last_recalled_at"))
        salience = float(result.get("salience_score", 0.0))

        # Compute age factor
        age_factor = compute_age_factor(
            created_at=created,
            last_recalled_at=recalled,
            salience_score=salience,
            now=now,
        )

        # Apply to score
        original_score = result.get("score", result.get("_distance", 0.5))
        if isinstance(original_score, (int, float)):
            faded_score = original_score * age_factor
        else:
            faded_score = age_factor

        faded_result = dict(result)
        faded_result["faded_score"] = faded_score
        faded_result["age_factor"] = age_factor
        faded.append(faded_result)

    # Sort by faded score (higher = more relevant)
    faded.sort(key=lambda r: r.get("faded_score", 0), reverse=True)
    return faded


def mark_recalled(memory_id: str, store=None):
    """Mark a memory as recently recalled — refreshes its decay clock.

    This should be called when hippocampus retrieves a memory and it's
    actually used in context. Simply being a search candidate doesn't count.

    Args:
        memory_id: The memory's unique identifier
        store: The memory store to update (should support update by ID)
    """
    if store:
        try:
            store.update_recall_timestamp(memory_id, time.time())
        except Exception as e:
            logger.debug("Failed to mark memory %s as recalled: %s", memory_id, e)


def _parse_timestamp(value) -> Optional[float]:
    """Parse a timestamp from various formats."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None
    return None
