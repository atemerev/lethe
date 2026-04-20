"""Lethe Console - Mind State Visualization.

A web-based dashboard showing the agent's current context assembly:
- Chat messages
- Memory blocks
- System prompt
- What's actually sent to the LLM
"""

import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import deque
import re

logger = logging.getLogger(__name__)


@dataclass
class ConsoleState:
    """Shared state for the console UI."""
    
    # Memory blocks (label -> block data)
    memory_blocks: Dict[str, Dict] = field(default_factory=dict)
    
    # Identity/system prompt
    identity: str = ""
    
    # Conversation summary
    summary: str = ""
    
    # Recent messages (role, content, timestamp)
    messages: List[Dict] = field(default_factory=list)
    
    # Last built context (what was sent to LLM)
    last_context: List[Dict] = field(default_factory=list)
    last_context_tokens: int = 0
    last_context_time: Optional[datetime] = None
    
    # Agent status
    status: str = "idle"  # idle, thinking, tool_call
    current_tool: Optional[str] = None
    
    # Stats
    total_messages: int = 0
    archival_count: int = 0
    
    # Model info
    model: str = ""
    model_aux: str = ""
    
    # Token tracking
    tokens_today: int = 0
    api_calls_today: int = 0
    prompt_tokens_today: int = 0
    completion_tokens_today: int = 0
    tokens_last_total: int = 0
    tokens_last_prompt: int = 0
    tokens_last_completion: int = 0
    tokens_per_hour: float = 0.0
    api_calls_per_hour: float = 0.0
    token_events: deque = field(default_factory=lambda: deque(maxlen=4096))
    token_totals_by_source: Dict[str, int] = field(default_factory=dict)
    anthropic_ratelimit: Dict[str, Any] = field(default_factory=dict)
    
    # Cache stats (from API response usage.prompt_tokens_details)
    cache_read_tokens: int = 0       # Total cached tokens read today
    cache_write_tokens: int = 0      # Total cached tokens written today
    last_cache_read: int = 0         # Cached tokens read in last request
    last_cache_write: int = 0        # Cached tokens written in last request
    last_prompt_tokens: int = 0      # Total prompt tokens in last request
    
    # Subsystem monitoring
    actor_system: Dict[str, Any] = field(default_factory=dict)
    dmn: Dict[str, Any] = field(default_factory=dict)
    amygdala: Dict[str, Any] = field(default_factory=dict)
    hippocampus: Dict[str, Any] = field(default_factory=dict)
    stem: Dict[str, Any] = field(default_factory=dict)
    dmn_context: str = ""
    amygdala_context: str = ""
    hippocampus_context: str = ""
    stem_context: str = ""
    
    # Change tracking (incremented on data changes that need UI rebuild)
    version: int = 0


# Global state instance
_state = ConsoleState()


_DATA_URL_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+", re.IGNORECASE)


def _sanitize_text_payload(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    # Replace embedded data URLs with a compact marker.
    return _DATA_URL_RE.sub("[image base64 omitted]", text)


def _sanitize_content(content):
    """Redact image/base64 payloads before storing console state."""
    if isinstance(content, str):
        return _sanitize_text_payload(content)
    if isinstance(content, list):
        sanitized = []
        for part in content:
            if not isinstance(part, dict):
                sanitized.append(part)
                continue
            p = dict(part)
            if p.get("type") == "image_url":
                image = p.get("image_url")
                if isinstance(image, dict):
                    url = str(image.get("url", ""))
                    if url.startswith("data:image/"):
                        image = dict(image)
                        image["url"] = "[image base64 omitted]"
                        p["image_url"] = image
                elif isinstance(image, str) and image.startswith("data:image/"):
                    p["image_url"] = "[image base64 omitted]"
            if p.get("type") == "text":
                p["text"] = _sanitize_text_payload(str(p.get("text", "")))
            sanitized.append(p)
        return sanitized
    if isinstance(content, dict):
        out = {}
        for k, v in content.items():
            out[k] = _sanitize_content(v)
        return out
    return content


def get_state() -> ConsoleState:
    """Get the global console state."""
    return _state


def update_memory_blocks(blocks: List[Dict]):
    """Update memory blocks in console state."""
    _state.memory_blocks = {b["label"]: b for b in blocks}
    _state.version += 1


def update_identity(identity: str):
    """Update identity/system prompt."""
    _state.identity = identity
    _state.version += 1


def update_summary(summary: str):
    """Update conversation summary."""
    if _state.summary != summary:
        _state.summary = summary
        _state.version += 1


def update_messages(messages):
    """Update recent messages.
    
    Args:
        messages: List of Message objects or dicts
    """
    result = []
    for msg in messages:
        if hasattr(msg, 'role'):
            # Message object
            timestamp = None
            if hasattr(msg, 'created_at') and msg.created_at:
                timestamp = msg.created_at.strftime("%H:%M:%S") if hasattr(msg.created_at, 'strftime') else str(msg.created_at)[:19]
            result.append({
                "role": msg.role,
                "content": _sanitize_content(msg.content) if isinstance(msg.content, str) or isinstance(msg.content, list) else str(msg.content),
                "timestamp": timestamp,
            })
        elif isinstance(msg, dict):
            safe_msg = dict(msg)
            safe_msg["content"] = _sanitize_content(safe_msg.get("content", ""))
            result.append(safe_msg)
    if _state.messages != result:
        _state.messages = result
        _state.version += 1


def update_context(context: List[Dict], tokens: int):
    """Update last built context."""
    sanitized_context = []
    for msg in (context or []):
        if isinstance(msg, dict):
            safe = dict(msg)
            safe["content"] = _sanitize_content(safe.get("content", ""))
            sanitized_context.append(safe)
        else:
            sanitized_context.append(msg)
    _state.last_context = sanitized_context
    _state.last_context_tokens = tokens
    _state.last_context_time = datetime.now()
    _state.version += 1


def update_status(status: str, tool: Optional[str] = None):
    """Update agent status."""
    _state.status = status
    _state.current_tool = tool


def update_stats(total_messages: int, archival_count: int):
    """Update stats."""
    if _state.total_messages != total_messages or _state.archival_count != archival_count:
        _state.total_messages = total_messages
        _state.archival_count = archival_count
        _state.version += 1


def update_model_info(model: str, model_aux: str = ""):
    """Update model info."""
    _state.model = model
    _state.model_aux = model_aux
    _state.version += 1


def track_tokens(tokens: int):
    """Track tokens consumed."""
    _state.tokens_today += tokens
    _state.api_calls_today += 1
    _state.tokens_last_total = tokens
    _state.token_events.append({
        "ts": datetime.now(timezone.utc).timestamp(),
        "total": tokens,
        "prompt": 0,
        "completion": 0,
        "source": "legacy",
    })
    _recompute_hourly()
    _state.version += 1


def _prune_events(now_ts: float):
    """Drop token events older than 1 hour."""
    cutoff = now_ts - 3600
    while _state.token_events and _state.token_events[0]["ts"] < cutoff:
        _state.token_events.popleft()


def _recompute_hourly():
    """Compute rolling per-hour token and call rates."""
    now_ts = datetime.now(timezone.utc).timestamp()
    _prune_events(now_ts)
    total = sum(int(e.get("total", 0)) for e in _state.token_events)
    calls = len(_state.token_events)
    _state.tokens_per_hour = float(total)
    _state.api_calls_per_hour = float(calls)


def track_usage(usage: dict, source: str = "", model: str = ""):
    """Track full token usage details from an API response."""
    if not usage:
        return
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0)
    if total <= 0:
        total = prompt + completion
    if total <= 0:
        return

    src = source or "unknown"
    now_ts = datetime.now(timezone.utc).timestamp()

    _state.tokens_today += total
    _state.prompt_tokens_today += prompt
    _state.completion_tokens_today += completion
    _state.api_calls_today += 1
    _state.tokens_last_total = total
    _state.tokens_last_prompt = prompt
    _state.tokens_last_completion = completion
    _state.token_totals_by_source[src] = _state.token_totals_by_source.get(src, 0) + total
    _state.token_events.append({
        "ts": now_ts,
        "total": total,
        "prompt": prompt,
        "completion": completion,
        "source": src,
        "model": model,
    })
    _recompute_hourly()
    _state.version += 1


def track_cache_usage(usage: dict):
    """Track cache usage from API response.
    
    Works with all providers via OpenRouter's unified format:
    - Anthropic: cache_creation_input_tokens, cache_read_input_tokens
    - OpenRouter unified: prompt_tokens_details.cached_tokens, cache_write_tokens
    - Moonshot/Kimi: automatic caching, same unified format
    """
    # Keep one normalized pair per request to avoid double counting when both
    # provider-native and unified fields are present in the same payload.
    details = usage.get("prompt_tokens_details", {}) or {}
    direct_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    direct_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    unified_read = int(details.get("cached_tokens", 0) or 0)
    unified_write = int(details.get("cache_write_tokens", 0) or 0)

    cache_read = direct_read if direct_read > 0 else unified_read
    cache_write = direct_write if direct_write > 0 else unified_write

    _state.last_cache_read = cache_read
    _state.last_cache_write = cache_write
    if cache_read:
        _state.cache_read_tokens += cache_read
    if cache_write:
        _state.cache_write_tokens += cache_write

    _state.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)


def update_actor_status(status: dict):
    """Update actor-system monitoring details."""
    status = status or {}
    stem = status.get("brainstem", {})
    dmn = status.get("dmn", {})
    amygdala = status.get("amygdala", {})
    if (
        _state.actor_system != status
        or _state.stem != stem
        or _state.dmn != dmn
        or _state.amygdala != amygdala
    ):
        _state.actor_system = status
        _state.stem = stem
        _state.dmn = dmn
        _state.amygdala = amygdala
        _state.version += 1


def update_hippocampus(stats: dict):
    """Update hippocampus monitoring details."""
    stats = stats or {}
    if _state.hippocampus != stats:
        _state.hippocampus = stats
        _state.version += 1


def update_dmn_context(context: str):
    """Update DMN context/debug view text."""
    context = context or ""
    if _state.dmn_context != context:
        _state.dmn_context = context
        _state.version += 1


def update_hippocampus_context(context: str):
    """Update hippocampus context/debug view text."""
    context = context or ""
    if _state.hippocampus_context != context:
        _state.hippocampus_context = context
        _state.version += 1


def update_amygdala_context(context: str):
    """Update amygdala context/debug view text."""
    context = context or ""
    if _state.amygdala_context != context:
        _state.amygdala_context = context
        _state.version += 1



def update_stem_context(context: str):
    """Update brainstem context/debug view text."""
    context = context or ""
    if _state.stem_context != context:
        _state.stem_context = context
        _state.version += 1


def update_anthropic_ratelimit(snapshot: dict):
    """Update latest Anthropic unified ratelimit snapshot."""
    snapshot = snapshot or {}
    if _state.anthropic_ratelimit != snapshot:
        _state.anthropic_ratelimit = snapshot
        _state.version += 1
