"""Direct LLM client with context management.

Uses litellm for multi-provider support (OpenRouter, Anthropic, OpenAI, etc).
Handles context preparation, token counting, and tool calling.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Union
import logging

import litellm
from litellm import acompletion, completion

from lethe.utils import strip_model_tags
from lethe.memory.anthropic_oauth import AnthropicOAuth, is_oauth_available
from lethe.prompts import load_prompt_template

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True
# Allow litellm to modify params for provider compatibility (e.g., Anthropic tool handling)
litellm.modify_params = True

# Debug logging for LLM interactions
LLM_DEBUG = os.environ.get("LLM_DEBUG", "false").lower() == "true"
LLM_DEBUG_DIR = Path(os.environ.get("LLM_DEBUG_DIR", "logs/llm"))


def _log_llm_interaction(request: Dict, response: Dict, label: str = "chat"):
    """Log LLM request/response to debug directory."""
    if not LLM_DEBUG:
        return
    
    try:
        LLM_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filepath = LLM_DEBUG_DIR / f"{timestamp}_{label}.json"
        
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "request": request,
            "response": response,
        }
        
        filepath.write_text(json.dumps(log_data, indent=2, default=str))
        logger.debug(f"Logged LLM interaction to {filepath}")
    except Exception as e:
        logger.warning(f"Failed to log LLM interaction: {e}")

# Provider configurations
# Models updated 2026-02
PROVIDERS = {
    "openrouter": {
        "env_key": "OPENROUTER_API_KEY",
        "model_prefix": "openrouter/",
        "default_model": "openrouter/moonshotai/kimi-k2.5-0127",
        "default_model_aux": "openrouter/qwen/qwen3-coder-next",
    },
    "anthropic": {
        "env_key": "ANTHROPIC_API_KEY",
        "model_prefix": "",  # litellm auto-detects claude models
        "default_model": "claude-opus-4-5-20251101",  # Claude Opus 4.5
        "default_model_aux": "claude-haiku-4-5-20251001",  # Claude Haiku 4.5
    },
    "openai": {
        "env_key": "OPENAI_API_KEY",
        "model_prefix": "",  # litellm auto-detects gpt models
        "default_model": "gpt-5.2",
        "default_model_aux": "gpt-5.2-mini",
    },
}

DEFAULT_PROVIDER = "openrouter"

# Context limits (chars, roughly 4 chars per token)
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_LIMIT = 128000  # tokens
DEFAULT_MAX_OUTPUT = 8000  # tokens

# Context budget management (Letta-style)
TOKEN_SAFETY_MARGIN = 1.3  # Safety margin for approximate token counting
SLIDING_WINDOW_KEEP_RATIO = 0.7  # Keep 70% of context after compaction
COMPACTION_TRIGGER_RATIO = 0.85  # Trigger compaction at 85% capacity
SUMMARY_MAX_LINES = 30  # Max summary lines (truncate by lines, not chars)

# Minimal heartbeat system prompt (template)
HEARTBEAT_SYSTEM_PROMPT = load_prompt_template(
    "llm_heartbeat_system",
    fallback="You are background reflection. Reply with ok unless urgent.",
)

# Letta-style summarization prompt (template)
SUMMARIZE_PROMPT = load_prompt_template(
    "llm_summarize",
    fallback="Summarize conversation concisely. Output summary only.",
)


@dataclass
class LLMConfig:
    """LLM configuration with multi-provider support via litellm."""
    provider: str = ""  # Auto-detect if not set
    model: str = ""  # Use provider default if not set
    model_aux: str = ""  # Auxiliary model for heartbeats, summarization (empty = use main)
    api_base: str = ""  # Custom API base URL for local/compatible providers
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    max_output_tokens: int = DEFAULT_MAX_OUTPUT
    temperature: float = 0.7

    
    def __post_init__(self):
        # Auto-detect provider from environment if not set
        if not self.provider:
            self.provider = self._detect_provider()
        
        provider_config = PROVIDERS.get(self.provider)
        if not provider_config:
            raise ValueError(f"Unknown provider: {self.provider}. Valid: {list(PROVIDERS.keys())}")
        
        prefix = provider_config["model_prefix"]
        
        # Set model from provider default if not set
        if not self.model:
            self.model = provider_config["default_model"]
        else:
            # Add provider prefix if needed (for litellm)
            if prefix and not self.model.startswith(prefix):
                self.model = prefix + self.model
        
        # Set aux model (for heartbeats, summarization)
        if not self.model_aux:
            self.model_aux = provider_config.get("default_model_aux", self.model)
        else:
            if prefix and not self.model_aux.startswith(prefix):
                self.model_aux = prefix + self.model_aux
        
        # Verify API key exists (ANTHROPIC_AUTH_TOKEN is an alternative for Anthropic)
        env_key = provider_config.get("env_key")
        if env_key and not os.environ.get(env_key):
            if self.provider == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                pass  # Bearer auth via ANTHROPIC_AUTH_TOKEN
            else:
                raise ValueError(f"{env_key} not set")
        
        logger.info(f"LLM config: provider={self.provider}, model={self.model}, aux={self.model_aux}")
    
    def _detect_provider(self) -> str:
        """Auto-detect provider from available API keys."""
        # Check LLM_PROVIDER env var first
        provider = os.environ.get("LLM_PROVIDER", "").lower()
        if provider and provider in PROVIDERS:
            return provider
        
        # Check for API keys in order of preference (skip OAuth providers)
        for name, config in PROVIDERS.items():
            env_key = config.get("env_key")
            if env_key and os.environ.get(env_key):
                logger.info(f"Auto-detected provider: {name}")
                return name
        
        # ANTHROPIC_AUTH_TOKEN as fallback for Anthropic (Bearer auth)
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            logger.info("Auto-detected provider: anthropic (via ANTHROPIC_AUTH_TOKEN)")
            return "anthropic"
        
        # Default
        return DEFAULT_PROVIDER
    



@dataclass
class Message:
    """A conversation message."""
    role: str  # system, user, assistant, tool
    content: Union[str, List[Dict]]  # str or multimodal content list
    created_at: Optional[datetime] = None  # timestamp for context display
    name: Optional[str] = None  # for tool messages
    tool_call_id: Optional[str] = None  # for tool results
    tool_calls: Optional[List[Dict]] = None  # for assistant tool calls
    
    @staticmethod
    def _sanitize_tool_id(tid: str) -> str:
        """Sanitize tool call ID for Anthropic (must match ^[a-zA-Z0-9-]+$)."""
        import re
        return re.sub(r'[^a-zA-Z0-9-]', '-', tid)
    
    def __post_init__(self):
        """Set created_at if not provided."""
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
    
    def get_text_content(self) -> str:
        """Get text content for token counting and logging."""
        if isinstance(self.content, str):
            return self.content
        # Multimodal: extract text parts
        texts = []
        for part in self.content:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                texts.append("[Image]")
        return " ".join(texts)
    
    def format_timestamp(self) -> str:
        """Format timestamp for context display."""
        if self.created_at:
            dt = self.created_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return dt.strftime("%a %Y-%m-%d %H:%M:%S UTC")
        return ""


@dataclass 
class ContextWindow:
    """Manages context window with automatic summarization.
    
    Structure:
    1. System prompt (fixed)
    2. Memory blocks (fixed, from BlockManager)  
    3. Conversation summary (compressed old messages)
    4. Recent messages (sliding window)
    """
    system_prompt: str
    memory_context: str  # Formatted memory blocks
    messages: List[Message] = field(default_factory=list)
    config: LLMConfig = field(default_factory=LLMConfig)
    summary: str = ""  # Summary of older messages
    total_messages_db: int = 0  # Total messages in database (set by caller)
    system_prompt_loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    memory_context_updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    transient_system_context: str = ""  # Per-turn synthetic context (e.g. recall), injected in system role
    _summarizer: Optional[Callable] = None  # Set by LLMClient
    _tool_reference: str = ""  # Compact tool list for system prompt (non-Anthropic models)
    _tool_schemas_text: str = ""  # Serialized tool schemas for token budget accounting
    
    def count_tokens(self, text: str) -> int:
        """Approximate token count with safety margin.
        
        Uses 4 chars per token approximation with 1.3x safety margin
        to avoid underestimating (Letta's approach).
        """
        base_count = len(text) // CHARS_PER_TOKEN
        return int(base_count * TOKEN_SAFETY_MARGIN)
    
    def get_fixed_tokens(self) -> int:
        """Get tokens used by fixed content (including tool schemas)."""
        return (
            self.count_tokens(self.system_prompt) +
            self.count_tokens(self.memory_context) +
            self.count_tokens(self.summary) +
            self.count_tokens(self.transient_system_context) +
            self.count_tokens(self._tool_reference) +
            self.count_tokens(self._tool_schemas_text)
        )
    
    def get_available_tokens(self) -> int:
        """Get tokens available for messages."""
        fixed_tokens = self.get_fixed_tokens()
        # Reserve space for output
        available = self.config.context_limit - fixed_tokens - self.config.max_output_tokens
        return max(0, available)
    
    def add_message(self, message: Message):
        """Add a message, summarizing old ones if needed."""
        self.messages.append(message)
        self._compress_if_needed()

    @staticmethod
    def _format_block_timestamp(value: Optional[datetime]) -> str:
        """Format block timestamps with weekday and explicit UTC marker."""
        if not value:
            return ""
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%a %Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def _xml_attr(value: Any) -> str:
        """Escape XML attribute values."""
        return html_escape(str(value), quote=True)

    @classmethod
    def _render_system_block(
        cls,
        tag: str,
        content: str,
        timestamp: Optional[datetime] = None,
        extra_attrs: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Render a cleanly marked system block."""
        attrs = dict(extra_attrs or {})
        ts = cls._format_block_timestamp(timestamp)
        if ts:
            attrs["timestamp"] = ts
        attr_str = "".join(f' {k}="{cls._xml_attr(v)}"' for k, v in attrs.items())
        return f"<{tag}{attr_str}>\n{content}\n</{tag}>"

    def _wrap_conversation_text_block(self, msg: Message, content: str) -> str:
        """Wrap assistant/user/tool content in clean markup with timestamps."""
        role_map = {
            "assistant": "assistant_block",
            "user": "user_block",
            "tool": "tool_block",
        }
        tag = role_map.get(msg.role, "message_block")
        attrs: Dict[str, Any] = {}
        ts = msg.format_timestamp()
        if ts:
            attrs["timestamp"] = ts
        if msg.role == "tool":
            if msg.name:
                attrs["tool"] = msg.name
            if msg.tool_call_id:
                attrs["tool_call_id"] = msg.tool_call_id
        return self._render_system_block(tag, content, None, attrs)

    def _wrap_conversation_multimodal_block(self, msg: Message, content: List[Dict]) -> List[Dict]:
        """Wrap multimodal user/assistant messages with timestamped text delimiters."""
        role_map = {
            "assistant": "assistant_block",
            "user": "user_block",
        }
        tag = role_map.get(msg.role, "message_block")
        attrs: Dict[str, Any] = {"multimodal": "true"}
        ts = msg.format_timestamp()
        if ts:
            attrs["timestamp"] = ts
        attr_str = "".join(f' {k}="{self._xml_attr(v)}"' for k, v in attrs.items())
        open_part = {"type": "text", "text": f"<{tag}{attr_str}>"}
        close_part = {"type": "text", "text": f"</{tag}>"}
        return [open_part] + list(content) + [close_part]

    def upsert_time_passed_block(self, minutes_passed: int):
        """Append or update a single idle-time marker in user role."""
        minutes = max(1, int(minutes_passed))
        now = datetime.now(timezone.utc)
        ts = self._format_block_timestamp(now)
        content = (
            f'<time_passed_block timestamp="{self._xml_attr(ts)}" minutes="{minutes}">\n'
            f"{minutes} minutes passed with no new user-visible events.\n"
            "</time_passed_block>"
        )

        if self.messages:
            last = self.messages[-1]
            if (
                last.role == "user"
                and isinstance(last.content, str)
                and "<time_passed_block " in last.content
            ):
                last.content = content
                last.created_at = now
                return

        self.add_message(Message(role="user", content=content, created_at=now))
    
    def load_messages(self, messages: List[dict]):
        """Load existing messages from history (e.g., from database).
        
        Args:
            messages: List of dicts with 'role', 'content', 'metadata', and optionally 'created_at' keys
        
        Tool messages from history are skipped — they can only be properly paired
        within the same session. Cross-model tool ID formats are incompatible.
        """
        loaded_count = 0
        skipped_empty = 0
        skipped_tool = 0
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            metadata = msg.get("metadata", {})
            
            # Skip tool messages from history — they can't be properly paired
            # and cause the orphan cleaner to strip their parent assistant messages
            if role == "tool":
                skipped_tool += 1
                continue
            
            # Strip tool_calls from loaded assistant messages — their results
            # aren't in history, so orphan cleanup would destroy these messages
            if role == "assistant" and metadata.get("tool_calls"):
                metadata = {k: v for k, v in metadata.items() if k != "tool_calls"}
            
            # Handle multimodal content - extract text, skip base64
            if isinstance(content, str) and content.startswith("["):
                try:
                    import json
                    parsed = json.loads(content)
                    if isinstance(parsed, list):
                        text_parts = []
                        for p in parsed:
                            if isinstance(p, dict):
                                if p.get("type") == "text":
                                    text_parts.append(p.get("text", ""))
                                elif p.get("type") == "image_url":
                                    # Skip base64 images, just note they existed
                                    text_parts.append("[image]")
                        content = " ".join(text_parts)
                except (json.JSONDecodeError, TypeError):
                    pass
            
            # Skip huge messages (likely base64 or other binary content)
            if len(str(content)) > 50000:
                content = f"[large content: {len(str(content))} chars]"
            
            # Skip assistant messages that have no content and no tool_calls
            if role == "assistant" and not content and not metadata.get("tool_calls"):
                skipped_empty += 1
                continue
            
            # Parse created_at from database
            created_at = None
            if msg.get("created_at"):
                try:
                    created_at = datetime.fromisoformat(msg["created_at"].replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    created_at = datetime.now(timezone.utc)
            
            # Build message with all metadata
            self.messages.append(Message(
                role=role,
                content=content,
                created_at=created_at,
                tool_call_id=metadata.get("tool_call_id"),
                tool_calls=metadata.get("tool_calls"),
                name=metadata.get("name"),
            ))
            loaded_count += 1
        
        logger.info(f"Loaded {loaded_count} messages (skipped: {skipped_tool} tool, {skipped_empty} empty)")
        # Compress if needed after loading
        self._compress_if_needed()
    
    def _compress_if_needed(self):
        """Summarize oldest messages using Letta-style sliding window.
        
        Approach:
        1. Trigger when messages exceed COMPACTION_TRIGGER_RATIO (85%) of available tokens
        2. Find cutoff point by TOKEN budget (not message count) to hit SLIDING_WINDOW_KEEP_RATIO
        3. Cutoff must be at assistant message boundary (avoid mid-conversation splits)
        4. Summarize messages before cutoff, keep messages after
        """
        available = self.get_available_tokens()
        total = sum(self.count_tokens(m.get_text_content()) for m in self.messages)
        
        # Check if compaction needed
        if total <= available * COMPACTION_TRIGGER_RATIO or len(self.messages) <= 4:
            return
        
        logger.info(f"Context compaction triggered: {total} tokens > {available * COMPACTION_TRIGGER_RATIO:.0f} threshold ({len(self.messages)} messages)")
        
        # Target: keep enough messages to fit within KEEP_RATIO of available tokens
        target_tokens = int(available * SLIDING_WINDOW_KEEP_RATIO)
        
        # Walk backwards from end, accumulating tokens, to find how many messages to keep
        kept_tokens = 0
        keep_from = len(self.messages)  # Start from end
        for i in range(len(self.messages) - 1, -1, -1):
            msg_tokens = self.count_tokens(self.messages[i].get_text_content())
            if kept_tokens + msg_tokens > target_tokens:
                keep_from = i + 1
                break
            kept_tokens += msg_tokens
        else:
            keep_from = 0
        
        # Ensure we keep at least 2 messages
        keep_from = min(keep_from, len(self.messages) - 2)
        
        # Adjust cutoff to a clean boundary: must not split tool_call/tool_result pairs.
        # A safe cutoff is a position where:
        # - The message BEFORE cutoff is NOT an assistant with tool_calls (would orphan results)
        # - The message AT cutoff is NOT a tool result (would orphan from its assistant)
        cutoff = keep_from
        while cutoff > 0 and cutoff < len(self.messages):
            msg_at = self.messages[cutoff]
            msg_before = self.messages[cutoff - 1]
            
            # NOT safe: cutting right after an assistant with tool_calls (results follow)
            if msg_before.role == "assistant" and msg_before.tool_calls:
                cutoff -= 1
                continue
            # NOT safe: cutting where a tool result would be separated from its assistant
            if msg_at.role == "tool":
                cutoff -= 1
                continue
            # Safe: clean boundary (user msg, plain assistant, etc.)
            break
        
        if cutoff <= 0:
            cutoff = keep_from  # Fallback if no clean boundary found
        
        to_summarize = self.messages[:cutoff]
        self.messages = self.messages[cutoff:]
        
        kept_tokens_actual = sum(self.count_tokens(m.get_text_content()) for m in self.messages)
        logger.info(f"Compaction: {total} → {kept_tokens_actual} tokens (removed {len(to_summarize)} messages, kept {len(self.messages)})")
        
        logger.info(f"Compacting: summarizing {len(to_summarize)} messages, keeping {len(self.messages)}")
        
        if self._summarizer and to_summarize:
            new_summary = self._summarizer(to_summarize, self.summary)
            if new_summary:
                # Truncate by lines if too long
                lines = new_summary.strip().split("\n")
                if len(lines) > SUMMARY_MAX_LINES:
                    new_summary = "\n".join(lines[:SUMMARY_MAX_LINES]) + f"\n[...truncated, {len(lines) - SUMMARY_MAX_LINES} more lines]"
                self.summary = new_summary
                logger.info(f"Summary updated: {len(self.summary)} chars, {len(lines)} lines")
        else:
            # Fallback: text-based summary
            old_text = "\n".join(f"{m.role}: {m.get_text_content()[:200]}" for m in to_summarize[-5:])
            if self.summary:
                self.summary = f"{self.summary}\n[+{len(to_summarize)} messages]\n{old_text}"
            else:
                self.summary = f"[Summary of {len(to_summarize)} messages]\n{old_text}"
            # Truncate by lines
            lines = self.summary.strip().split("\n")
            if len(lines) > SUMMARY_MAX_LINES:
                self.summary = "\n".join(lines[:SUMMARY_MAX_LINES]) + f"\n[...truncated, {len(lines) - SUMMARY_MAX_LINES} more lines]"
    
    def get_stats(self) -> dict:
        """Get context window statistics."""
        message_tokens = sum(self.count_tokens(m.get_text_content()) for m in self.messages)
        fixed_tokens = self.get_fixed_tokens()
        available = self.get_available_tokens()
        total_used = fixed_tokens + message_tokens
        
        return {
            "context_limit": self.config.context_limit,
            "fixed_tokens": fixed_tokens,
            "message_tokens": message_tokens,
            "total_used": total_used,
            "available": available,
            "utilization": f"{(total_used / self.config.context_limit * 100):.1f}%",
            "message_count": len(self.messages),
            "total_messages_db": self.total_messages_db,
            "summary_chars": len(self.summary),
            "compaction_threshold": f"{COMPACTION_TRIGGER_RATIO * 100:.0f}%",
            "keep_ratio": f"{SLIDING_WINDOW_KEEP_RATIO * 100:.0f}%",
        }
    
    def _clean_orphaned_tool_messages(self):
        """Ensure tool_use/tool_result pairing is correct for Anthropic.
        
        - Remove tool_result without matching tool_use
        - Remove assistant tool_calls if no tool_results follow
        """
        if not self.messages:
            return
        
        # First pass: collect valid tool_call_id → result pairs
        # Walk forward to check which tool_uses have results
        tool_use_to_results = {}  # assistant_idx → set of tool_call_ids that got results
        current_assistant_idx = None
        current_tool_ids = set()
        
        for i, msg in enumerate(self.messages):
            if msg.role == "assistant" and msg.tool_calls:
                # Save previous unresolved assistant
                if current_assistant_idx is not None:
                    tool_use_to_results[current_assistant_idx] = current_tool_ids.copy()
                current_assistant_idx = i
                current_tool_ids = set()
            elif msg.role == "tool" and msg.tool_call_id and current_assistant_idx is not None:
                current_tool_ids.add(msg.tool_call_id)
            elif msg.role in ("user", "assistant") and not (msg.role == "assistant" and msg.tool_calls):
                if current_assistant_idx is not None:
                    tool_use_to_results[current_assistant_idx] = current_tool_ids.copy()
                current_assistant_idx = None
                current_tool_ids = set()
        # Don't forget the last group
        if current_assistant_idx is not None:
            tool_use_to_results[current_assistant_idx] = current_tool_ids.copy()
        
        # Second pass: build clean message list
        clean_messages = []
        expected_tool_ids = set()
        
        for i, msg in enumerate(self.messages):
            if msg.role == "assistant" and msg.tool_calls:
                # Check if this assistant's tool_calls have results
                result_ids = tool_use_to_results.get(i, set())
                expected_ids = {tc["id"] for tc in msg.tool_calls}
                
                if not result_ids:
                    # No results at all — strip tool_calls, keep text if any
                    if msg.content:
                        clean_messages.append(Message(
                            role="assistant", content=msg.content,
                            created_at=msg.created_at,
                        ))
                    else:
                        logger.warning(f"Removing assistant tool_calls with no results: {expected_ids}")
                    expected_tool_ids = set()
                else:
                    if result_ids != expected_ids:
                        # Partial results — filter tool_calls to matched ones only
                        filtered_calls = [tc for tc in msg.tool_calls if tc["id"] in result_ids]
                        if filtered_calls:
                            clean_messages.append(Message(
                                role="assistant",
                                content=msg.content,
                                created_at=msg.created_at,
                                tool_calls=filtered_calls,
                            ))
                        elif msg.content:
                            clean_messages.append(Message(
                                role="assistant", content=msg.content,
                                created_at=msg.created_at,
                            ))
                        else:
                            logger.warning(f"Removing assistant with partial tool results: {expected_ids - result_ids}")
                        expected_tool_ids = result_ids
                    else:
                        clean_messages.append(msg)
                        expected_tool_ids = expected_ids
            elif msg.role == "tool" and msg.tool_call_id:
                if msg.tool_call_id in expected_tool_ids:
                    clean_messages.append(msg)
                    expected_tool_ids.discard(msg.tool_call_id)
                else:
                    logger.warning(f"Removing orphaned tool result: {msg.tool_call_id}")
            else:
                expected_tool_ids = set()
                clean_messages.append(msg)
        
        if len(clean_messages) != len(self.messages):
            logger.info(f"Cleaned {len(self.messages) - len(clean_messages)} orphaned tool messages")
            self.messages = clean_messages
    
    def _build_tool_reference(self, tools: List[Dict]) -> str:
        """Build compact tool reference for embedding in system prompt.
        
        Some models (Kimi K2.5) work better when tools are visible in context
        text rather than only in the separate tools parameter.
        """
        if not tools:
            return ""
        
        lines = ["<available_tools>"]
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "?")
            desc = func.get("description", "").split("\n")[0]  # First line only
            params = func.get("parameters", {}).get("properties", {})
            param_names = ", ".join(params.keys())
            lines.append(f"- **{name}**({param_names}): {desc}")
        lines.append("</available_tools>")
        return "\n".join(lines)
    
    def build_messages(self) -> List[Dict]:
        """Build messages array for API call (Letta-style structure with prompt caching)."""
        # Clean orphaned tool messages before building
        self._clean_orphaned_tool_messages()
        
        # Build system prompt blocks with explicit markup.
        # Anthropic: separate blocks for cache-friendly behavior
        #   1. Identity/system (rarely changes) — cached 1h
        #   2. Memory context (changes on memory updates) — cached 5m
        #   3. Tool reference (non-Anthropic models only)
        #   4. Summary + runtime context (volatile) — uncached
        # Cache order: tools → system blocks (prefix-matched)
        # Each cache_control creates a breakpoint: content before it is cached independently
        
        is_anthropic = "claude" in self.config.model.lower() or "anthropic" in self.config.model.lower()

        identity_block = self._render_system_block(
            "identity_block",
            self.system_prompt,
            self.system_prompt_loaded_at,
        )
        memory_block = self._render_system_block(
            "memory_context_block",
            self.memory_context,
            self.memory_context_updated_at,
            {"kind": "core_memory"},
        ) if self.memory_context else ""
        summary_block = self._render_system_block(
            "conversation_summary_block",
            self.summary,
            datetime.now(timezone.utc),
        ) if self.summary else ""
        runtime_block = self._render_system_block(
            "runtime_context_block",
            self.transient_system_context,
            datetime.now(timezone.utc),
            {"source": "runtime"},
        ) if self.transient_system_context else ""

        if is_anthropic:
            system_content = []
            
            # Block 1: identity/system instructions (stable)
            system_content.append({
                "type": "text",
                "text": identity_block,
                "cache_control": {"type": "ephemeral", "ttl": "1h"}
            })
            
            # Block 2: memory context (volatile but cacheable)
            if memory_block:
                system_content.append({
                    "type": "text",
                    "text": memory_block,
                    "cache_control": {"type": "ephemeral"}
                })
            
            # Block 3: conversation summary (uncached)
            if summary_block:
                system_content.append({
                    "type": "text",
                    "text": summary_block
                })

            # Block 4: runtime/synthetic context (uncached)
            if runtime_block:
                system_content.append({
                    "type": "text",
                    "text": runtime_block,
                })
            
            messages = [{"role": "system", "content": system_content}]
        else:
            # Non-Anthropic: structured content blocks with cache_control.
            # LiteLLM translates cache_control directives per provider
            # (OpenAI/Deepseek: automatic prefix caching, Gemini: context caching).
            system_content = []
            
            # Block 1: identity/system instructions (stable, cacheable)
            system_content.append({
                "type": "text",
                "text": identity_block,
                "cache_control": {"type": "ephemeral"},
            })
            
            # Block 2: memory context (volatile but cacheable)
            if memory_block:
                tools_text = ""
                if self._tool_reference:
                    tools_text = "\n\n" + self._render_system_block(
                        "available_tools_block",
                        self._tool_reference,
                        self.system_prompt_loaded_at,
                    )
                system_content.append({
                    "type": "text",
                    "text": memory_block + tools_text,
                    "cache_control": {"type": "ephemeral"},
                })
            
            # Block 3: summary (uncached, changes frequently)
            if summary_block:
                system_content.append({
                    "type": "text",
                    "text": summary_block,
                })
            
            # Block 4: runtime context (uncached, per-turn)
            if runtime_block:
                system_content.append({
                    "type": "text",
                    "text": runtime_block,
                })
            
            messages = [{"role": "system", "content": system_content}]

        # Ensure timeline order for all non-system messages.
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        timeline_messages = sorted(
            self.messages,
            key=lambda m: (
                (m.created_at.replace(tzinfo=timezone.utc) if m.created_at and m.created_at.tzinfo is None
                 else (m.created_at.astimezone(timezone.utc) if m.created_at else epoch))
            ),
        )

        # Find indices of image messages (to keep only most recent)
        image_indices = []
        for i, msg in enumerate(timeline_messages):
            if isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        image_indices.append(i)
                        break
        
        # Keep only last 2 images, replace older ones with placeholders
        old_image_indices = set(image_indices[:-2]) if len(image_indices) > 2 else set()
        
        # Find heartbeat messages (keep only the last one)
        heartbeat_indices = []
        for i, msg in enumerate(timeline_messages):
            if msg.role == "user" and isinstance(msg.content, str):
                if msg.content.startswith("[System Heartbeat"):
                    heartbeat_indices.append(i)
        # Skip all but the last heartbeat (and its response)
        old_heartbeat_indices = set(heartbeat_indices[:-1]) if len(heartbeat_indices) > 1 else set()

        # Keep only the latest synthetic idle marker.
        time_passed_indices = []
        for i, msg in enumerate(timeline_messages):
            if msg.role == "user" and isinstance(msg.content, str) and "<time_passed_block " in msg.content:
                time_passed_indices.append(i)
        old_time_passed_indices = set(time_passed_indices[:-1]) if len(time_passed_indices) > 1 else set()
        
        # Find the last tool_call group (assistant with tool_calls + its tool results)
        # Only the last group gets full content; older tool results are truncated to metadata
        last_tool_assistant_idx = None
        for i in range(len(timeline_messages) - 1, -1, -1):
            if timeline_messages[i].role == "assistant" and timeline_messages[i].tool_calls:
                last_tool_assistant_idx = i
                break
        
        # Collect tool_call_ids from the last tool group
        last_tool_ids = set()
        if last_tool_assistant_idx is not None:
            for tc in timeline_messages[last_tool_assistant_idx].tool_calls:
                last_tool_ids.add(tc.get("id", ""))
        
        # Build role-separated timeline blocks.
        skip_next_assistant = False
        for i, msg in enumerate(timeline_messages):
            # Skip old heartbeats and their responses
            if i in old_heartbeat_indices:
                skip_next_assistant = True
                continue
            if skip_next_assistant and msg.role == "assistant":
                skip_next_assistant = False
                continue
            skip_next_assistant = False

            # Keep only the latest "time passed" marker block.
            if i in old_time_passed_indices:
                continue
            
            content = msg.content
            
            # Replace old images with placeholder text
            if i in old_image_indices and isinstance(content, list):
                # Extract image path from text part
                path = "image"
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if "Image from" in text:
                            path = text.replace("[Image from ", "").replace("]", "").strip()
                            break
                content = f"[Previously viewed image: {path}]"
            
            # Skim old tool results — keep full content only for the last tool_call group
            if msg.role == "tool" and msg.tool_call_id and msg.tool_call_id not in last_tool_ids:
                content_str = content if isinstance(content, str) else str(content)
                lines = content_str.split("\n")
                if len(lines) > 5:
                    tool_name = msg.name or "tool"
                    header = f"[{tool_name} output: {len(lines)} lines, {len(content_str)} chars — skipped]"
                    preview = "\n".join(lines[:5])
                    content = f"{header}\n{preview}\n[... {len(lines) - 5} more lines skipped]"
            
            # Cap oversized messages (e.g. PDF content pasted by user)
            MAX_MESSAGE_CHARS = 50000
            content_str = content if isinstance(content, str) else str(content)
            if len(content_str) > MAX_MESSAGE_CHARS:
                original_len = len(content_str)
                # Keep first and last portions for context
                keep = MAX_MESSAGE_CHARS - 200  # room for notice
                head = content_str[:keep // 2]
                tail = content_str[-(keep // 2):]
                content = f"{head}\n\n[... {original_len - keep:,} chars truncated ...]\n\n{tail}"
                logger.warning(f"Truncated oversized message ({original_len:,} → {MAX_MESSAGE_CHARS:,} chars)")

            # Wrap conversation content in clean role/timestamp markup.
            if isinstance(content, str):
                if msg.role == "assistant" and msg.tool_calls and not content.strip():
                    content = self._wrap_conversation_text_block(msg, "<tool_call_scaffold />")
                else:
                    content = self._wrap_conversation_text_block(msg, content)
            elif isinstance(content, list) and msg.role in ("user", "assistant"):
                content = self._wrap_conversation_multimodal_block(msg, content)
            
            m = {"role": msg.role, "content": content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            messages.append(m)
        
        # Merge consecutive assistant messages (confuses models about turn structure)
        merged = []
        for m in messages:
            if (merged 
                and m["role"] == "assistant" and not m.get("tool_calls")
                and merged[-1]["role"] == "assistant" and not merged[-1].get("tool_calls")):
                # Merge into previous
                prev_content = merged[-1].get("content") or ""
                cur_content = m.get("content") or ""
                if prev_content and cur_content:
                    merged[-1]["content"] = prev_content + "\n" + cur_content
                elif cur_content:
                    merged[-1]["content"] = cur_content
            else:
                merged.append(m)
        
        # Anthropic message caching: mark a message near the end with cache_control
        # Messages don't change individually, so everything before the breakpoint is cached
        # We cache the third-from-last message — new messages after it are uncached
        if is_anthropic and len(merged) > 3:
            cache_idx = len(merged) - 3  # 3rd from end
            cache_msg = merged[cache_idx]
            content = cache_msg.get("content", "")
            if isinstance(content, str) and content:
                cache_msg["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"}
                }]
            elif isinstance(content, list):
                # Already structured — add cache_control to last block
                if content:
                    content[-1]["cache_control"] = {"type": "ephemeral"}
        
        # Sanitize tool IDs for Anthropic (must match ^[a-zA-Z0-9-]+$)
        # Kimi needs exact format: functions.func_name:idx — DO NOT sanitize
        if is_anthropic:
            import re
            for m in merged:
                if m.get("tool_call_id"):
                    m["tool_call_id"] = re.sub(r'[^a-zA-Z0-9-]', '-', m["tool_call_id"])
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        if "id" in tc:
                            tc["id"] = re.sub(r'[^a-zA-Z0-9-]', '-', tc["id"])
            
            # Post-sanitization: validate tool_use/tool_result pairing
            # After merging and sanitizing, ensure every tool_result has a
            # matching tool_use in the immediately preceding assistant message
            valid = []
            current_tool_ids = set()
            for m in merged:
                if m["role"] == "assistant" and m.get("tool_calls"):
                    current_tool_ids = {tc["id"] for tc in m["tool_calls"]}
                    valid.append(m)
                elif m["role"] == "tool" and m.get("tool_call_id"):
                    if m["tool_call_id"] in current_tool_ids:
                        valid.append(m)
                    else:
                        logger.warning(f"Stripping unpaired tool_result after sanitization: {m.get('tool_call_id')}")
                else:
                    current_tool_ids = set()
                    valid.append(m)
            merged = valid
        
        return merged


class AsyncLLMClient:
    """Async LLM client with simple tool execution.
    
    Tools are just Python functions - schemas are auto-generated.
    No approval loop, no complex registration.
    """
    
    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        system_prompt: str = "",
        memory_context: str = "",
        on_message_persist: Optional[Callable] = None,  # Callback to persist messages
        usage_scope: str = "agent",
    ):
        self.config = config or LLMConfig()
        self.context = ContextWindow(
            system_prompt=system_prompt,
            memory_context=memory_context,
            config=self.config,
        )
        
        # Tools: name -> (function, schema)
        self._tools: Dict[str, tuple[Callable, Dict]] = {}
        
        # Persistence callback: (role, content, metadata) -> None
        self._on_message_persist = on_message_persist
        self._usage_scope = usage_scope or "agent"
        
        # Stop flag: set by terminate() tool to prevent extra API calls
        self._stop_after_tool = False
        
        # Set up summarizer callback
        self.context._summarizer = self._summarize_messages_sync
        
        # Auth mode: OAuth (subscription) takes priority over API key
        self._oauth: Optional[AnthropicOAuth] = None
        if self.config.provider == "anthropic" and is_oauth_available():
            self._oauth = AnthropicOAuth()
            has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            if has_api_key:
                logger.info("Auth: OAuth token AND API key both present — using OAuth (subscription)")
            else:
                logger.info("Auth: using OAuth token (Claude Max/Pro subscription)")
        elif self.config.provider == "anthropic":
            logger.info("Auth: using Anthropic API key")
        
        logger.info(f"AsyncLLMClient initialized with model {self.config.model}")
    
    # Tool results that should NOT be persisted to message history
    # (conversation_search results contain previous search results, creating recursive bloat)
    _SKIP_PERSIST_TOOLS = {"conversation_search", "archival_search"}
    
    def _add_and_persist(self, message: "Message"):
        """Add message to context and persist to storage."""
        self.context.add_message(message)
        
        if self._on_message_persist:
            # Skip persisting tool results from search tools (recursive bloat)
            if message.role == "tool" and message.name in self._SKIP_PERSIST_TOOLS:
                return
            
            metadata = {}
            if message.tool_call_id:
                metadata["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                metadata["tool_calls"] = message.tool_calls
            if message.name:
                metadata["name"] = message.name
            
            self._on_message_persist(message.role, message.content, metadata if metadata else None)
    
    def add_tool(self, func: Callable, schema: Optional[Dict] = None):
        """Add a tool function. Schema auto-generated if not provided."""
        from lethe.tools import function_to_schema
        
        if schema is None:
            schema = function_to_schema(func)
        
        self._tools[func.__name__] = (func, schema)
        self._update_tool_budget()
    
    def add_tools(self, tools: List[tuple[Callable, Dict]]):
        """Add multiple tools as (function, schema) tuples."""
        for func, schema in tools:
            # Use schema name as key (allows name overrides for async imports)
            name = schema.get("name", func.__name__)
            self._tools[name] = (func, schema)
        self._update_tool_budget()
    
    def _update_tool_budget(self):
        """Update context window's tool schema budget for accurate token counting."""
        if self.tools:
            self.context._tool_schemas_text = json.dumps(self.tools, separators=(',', ':'))
        else:
            self.context._tool_schemas_text = ""
    
    @property
    def tools(self) -> List[Dict]:
        """Get tool schemas for API calls."""
        return [
            {"type": "function", "function": schema}
            for _, schema in self._tools.values()
        ]
    
    def get_tool(self, name: str) -> Optional[Callable]:
        """Get tool function by name."""
        if name in self._tools:
            return self._tools[name][0]
        return None
    
    def _summarize_messages_sync(self, messages: List[Message], existing_summary: str) -> str:
        """Summarize messages (sync version for use in async context)."""
        # Format messages for summarization
        formatted = []
        for m in messages:
            formatted.append(f"{m.role}: {m.get_text_content()}")
        conversation = "\n".join(formatted)
        
        if existing_summary:
            conversation = f"Previous summary: {existing_summary}\n\nNew messages to incorporate:\n{conversation}"
        
        # Use litellm for summarization
        try:
            response = completion(
                model=self.config.model_aux,
                messages=[
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": conversation},
                ],
                temperature=0.3,
                max_tokens=500,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return existing_summary
    
    def register_tool(self, name: str, handler: Callable, schema: Dict):
        """Register a tool (legacy method, use add_tool instead)."""
        self._tools[name] = (handler, schema)
    
    def get_context_stats(self) -> dict:
        """Get context window statistics."""
        return self.context.get_stats()
    
    def update_memory_context(self, memory_context: str):
        """Update the memory context."""
        self.context.memory_context = memory_context
        self.context.memory_context_updated_at = datetime.now(timezone.utc)

    def note_idle_interval(self, minutes_passed: int):
        """Record idle passage of time as a single synthetic user block."""
        self.context.upsert_time_passed_block(minutes_passed)
    
    def set_console_hooks(
        self,
        on_context_build: Optional[Callable] = None,
        on_status_change: Optional[Callable] = None,
        on_token_usage: Optional[Callable] = None,
    ):
        """Set callbacks for console state updates."""
        self._on_context_build = on_context_build
        self._on_status_change = on_status_change
        self._on_token_usage = on_token_usage
    
    def _notify_status(self, status: str, tool: Optional[str] = None):
        """Notify console of status change."""
        if hasattr(self, "_on_status_change") and self._on_status_change:
            self._on_status_change(status, tool)
    
    def _notify_context(self, context: List[Dict], tokens: int):
        """Notify console of context build."""
        if hasattr(self, "_on_context_build") and self._on_context_build:
            self._on_context_build(context, tokens)
    
    def _track_usage(self, result: Dict, source: str = "", model: str = ""):
        """Track token usage and cache stats from API response."""
        usage = result.get("usage", {})
        total = usage.get("total_tokens", 0)
        source_name = source or self._usage_scope
        model_name = model or result.get("model", "") or self.config.model
        if total and hasattr(self, "_on_token_usage") and self._on_token_usage:
            payload = {
                "source": source_name,
                "model": model_name,
                "usage": usage,
            }
            try:
                self._on_token_usage(payload)
            except TypeError:
                self._on_token_usage(total)
        
        # Track cache stats in console
        if usage:
            try:
                from lethe.console import track_cache_usage, track_usage
                track_usage(usage, source=source_name, model=model_name)
                track_cache_usage(usage)
            except ImportError:
                pass

    @staticmethod
    def _extract_anthropic_ratelimit(headers: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Anthropic unified ratelimit headers into a structured snapshot."""
        if not headers:
            return {}
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        if "anthropic-ratelimit-unified-status" not in h:
            return {}

        def _as_float(value: str) -> Optional[float]:
            try:
                return float(value)
            except Exception:
                return None

        def _as_int(value: str) -> Optional[int]:
            try:
                return int(float(value))
            except Exception:
                return None

        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "unified_status": h.get("anthropic-ratelimit-unified-status", ""),
            "representative_claim": h.get("anthropic-ratelimit-unified-representative-claim", ""),
            "fallback_percentage": _as_float(h.get("anthropic-ratelimit-unified-fallback-percentage", "")),
            "unified_reset": _as_int(h.get("anthropic-ratelimit-unified-reset", "")),
            "five_hour": {
                "status": h.get("anthropic-ratelimit-unified-5h-status", ""),
                "reset": _as_int(h.get("anthropic-ratelimit-unified-5h-reset", "")),
                "utilization": _as_float(h.get("anthropic-ratelimit-unified-5h-utilization", "")),
            },
            "seven_day": {
                "status": h.get("anthropic-ratelimit-unified-7d-status", ""),
                "reset": _as_int(h.get("anthropic-ratelimit-unified-7d-reset", "")),
                "utilization": _as_float(h.get("anthropic-ratelimit-unified-7d-utilization", "")),
            },
        }

    def _track_provider_headers(self, result: Dict):
        """Track provider-specific response headers for supervision modules."""
        if not isinstance(result, dict):
            return
        headers = result.get("_response_headers")
        if not headers:
            return
        snapshot = self._extract_anthropic_ratelimit(headers)
        if not snapshot:
            return
        try:
            from lethe.console import update_anthropic_ratelimit

            update_anthropic_ratelimit(snapshot)
        except ImportError:
            pass
    
    def load_messages(self, messages: List[dict]):
        """Load existing messages from history into context.
        
        Args:
            messages: List of dicts with 'role', 'content', and optionally 'created_at' keys
                     Timestamps are prepended to content for temporal context.
        """
        self.context.load_messages(messages)
    
    async def chat(
        self,
        message: str,
        max_tool_iterations: int = 10,
        on_message: Optional[Callable] = None,
        on_image: Optional[Callable] = None,  # Callback for image attachments
    ) -> str:
        """Send a message and get response, handling tool calls.
        
        Args:
            message: User message
            max_tool_iterations: Max tool call loops per batch
            on_message: Optional callback for intermediate messages
            
        Returns:
            Final assistant response text
        """
        import asyncio
        
        MAX_CONTINUATION_DEPTH = 2  # Max auto-continues (total iterations = up to 3 * max_tool_iterations)
        MAX_TOOL_ERRORS = 8
        MAX_REPEATED_TOOL_CALLS = 4
        MAX_NO_PROGRESS_TURNS = 4
        total_tool_calls = 0  # Track across entire chat() including continuations
        continuation_depth = 0
        total_tool_errors = 0
        repeated_tool_call_streak = 0
        no_progress_turns = 0
        last_tool_signature = ""
        circuit_breaker_reason = ""
        
        # Add user message
        self.context.add_message(Message(role="user", content=message))
        
        while True:
            empty_count = 0  # Track consecutive empty responses per continuation batch
            for _ in range(max_tool_iterations):
                # Make API call
                response = await self._call_api()
                
                # Check for tool calls
                choice = response["choices"][0]
                assistant_msg = choice["message"]
                
                # Get content (might be present even with tool calls)
                content = strip_model_tags(assistant_msg.get("content") or "")
                
                # Handle tool calls
                tool_calls = assistant_msg.get("tool_calls")
                
                if tool_calls:
                    import uuid
                    empty_count = 0  # Reset on actual tool use
                    total_tool_calls += len(tool_calls)
                    for tc in tool_calls:
                        # Generate ID if missing (some models omit it)
                        if not tc.get("id"):
                            tc["id"] = f"call-{uuid.uuid4().hex[:12]}"
                
                if tool_calls:
                    # Add assistant message with tool calls (and persist)
                    self._add_and_persist(Message(
                        role="assistant",
                        content=content,
                        tool_calls=tool_calls,
                    ))
                    
                    # Execute tools and add results
                    # Collect images to inject AFTER all tool results (to not break tool pairing)
                    images_to_inject = []
                    turn_had_successful_tool = False
                    
                    for tool_call in tool_calls:
                        tool_name = tool_call["function"]["name"].strip()  # Strip whitespace (model quirk)
                        tool_id = tool_call["id"]
                        
                        # Parse tool arguments (may be malformed JSON from some models)
                        try:
                            tool_args = json.loads(tool_call["function"]["arguments"])
                        except json.JSONDecodeError as e:
                            logger.error(f"Tool {tool_name} has malformed JSON args: {e}")
                            total_tool_errors += 1
                            # Add error result and continue (and persist)
                            self._add_and_persist(Message(
                                role="tool",
                                content=f"Error: malformed tool arguments - {e}",
                                tool_call_id=tool_id,
                                name=tool_name,
                            ))
                            continue

                        signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, ensure_ascii=True)}"
                        if signature == last_tool_signature:
                            repeated_tool_call_streak += 1
                        else:
                            repeated_tool_call_streak = 1
                            last_tool_signature = signature
                        
                        logger.info(f"Executing tool: {tool_name}({list(tool_args.keys())})")
                        self._notify_status("tool_call", tool_name)
                        
                        # Execute tool (handle both sync and async)
                        # Sync tools run in thread executor to avoid blocking the event loop
                        handler = self.get_tool(tool_name)
                        if handler:
                            try:
                                if asyncio.iscoroutinefunction(handler):
                                    result = await handler(**tool_args)
                                else:
                                    loop = asyncio.get_event_loop()
                                    result = await loop.run_in_executor(
                                        None, lambda: handler(**tool_args)
                                    )
                            except Exception as e:
                                result = f"Error: {e}"
                                logger.error(f"Tool {tool_name} failed: {e}")
                        else:
                            result = f"Unknown tool: {tool_name}"
                        
                        logger.info(f"  Result: {str(result)[:100]}...")
                        if isinstance(result, str) and result.startswith("Error:"):
                            total_tool_errors += 1
                        elif isinstance(result, str) and result.startswith("Unknown tool:"):
                            total_tool_errors += 1
                        else:
                            turn_had_successful_tool = True
                        
                        # Check for image attachment in result (send to user)
                        if isinstance(result, dict) and "_image_attachment" in result:
                            img = result["_image_attachment"]
                            if on_image and img.get("path"):
                                await on_image(img["path"])
                            # Remove attachment from result for context
                            result_for_context = {k: v for k, v in result.items() if k != "_image_attachment"}
                            result = result_for_context
                        
                        # Check for image view in result (collect for injection after all tool results)
                        if isinstance(result, dict) and "_image_view" in result:
                            img = result["_image_view"]
                            if img.get("data") and img.get("mime_type"):
                                images_to_inject.append({
                                    "mime_type": img["mime_type"],
                                    "data": img["data"],
                                    "path": img.get("path", "image")
                                })
                            # Remove from result for context
                            result_for_context = {k: v for k, v in result.items() if k != "_image_view"}
                            result = result_for_context
                        
                        # Add tool result (and persist)
                        self._add_and_persist(Message(
                            role="tool",
                            content=str(result),
                            tool_call_id=tool_id,
                            name=tool_name,
                        ))

                        if total_tool_errors >= MAX_TOOL_ERRORS:
                            circuit_breaker_reason = (
                                f"tool_error_cap hit ({total_tool_errors} errors >= {MAX_TOOL_ERRORS})"
                            )
                            break
                        if repeated_tool_call_streak >= MAX_REPEATED_TOOL_CALLS:
                            circuit_breaker_reason = (
                                f"repeated_tool_call_cap hit ({repeated_tool_call_streak} >= {MAX_REPEATED_TOOL_CALLS})"
                            )
                            break

                    if turn_had_successful_tool:
                        # Send intermediate updates only after actual successful tool execution.
                        # This avoids "Let me..." progress spam when tools were proposed but not used.
                        if content and on_message:
                            await on_message(strip_model_tags(content))
                        no_progress_turns = 0
                    else:
                        no_progress_turns += 1
                        if no_progress_turns >= MAX_NO_PROGRESS_TURNS and not circuit_breaker_reason:
                            circuit_breaker_reason = (
                                f"no_progress_cap hit ({no_progress_turns} turns >= {MAX_NO_PROGRESS_TURNS})"
                            )
                    
                    # Inject images AFTER all tool results (Anthropic requires tool_result immediately after tool_use)
                    for image in images_to_inject:
                        multimodal_content = [
                            {"type": "text", "text": f"[Image from {image['path']}]"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{image['mime_type']};base64,{image['data']}"
                                }
                            }
                        ]
                        self.context.add_message(Message(
                            role="user",
                            content=multimodal_content,
                        ))
                        logger.info(f"Injected image into context: {image['path']}")
                    
                    # Check if a tool (e.g. terminate()) requested early stop
                    if self._stop_after_tool:
                        logger.info("Early stop requested by tool — skipping further API calls")
                        self._stop_after_tool = False
                        return content or "Done."

                    if circuit_breaker_reason:
                        logger.warning(f"Circuit breaker triggered: {circuit_breaker_reason}")
                        self.context.add_message(Message(
                            role="user",
                            content=(
                                "[Circuit breaker triggered. Stop tool exploration. "
                                "Respond with a concise partial report now. "
                                "If you are an actor, call terminate(result) with the partial report.]"
                            ),
                        ))
                        break
                    
                    continue  # Loop to get next response
                
                # No tool calls - we have final response
                if content:
                    self.context.add_message(Message(role="assistant", content=content))
                    return content
                
                # Empty response — model stuck
                empty_count += 1
                if empty_count >= 2:
                    logger.warning(f"Model returned {empty_count} consecutive empty responses, forcing final")
                    self.context.add_message(Message(
                        role="user",
                        content="[Respond to the user now with what you know.]"
                    ))
                    response = await self._call_api_no_tools()
                    content = strip_model_tags(response["choices"][0]["message"].get("content", ""))
                    if content:
                        self.context.add_message(Message(role="assistant", content=content))
                        return content
                    return "Done."
                
                logger.info(f"Empty response (total tools: {total_tool_calls}), nudging model to respond")
                self.context.add_message(Message(
                    role="user",
                    content="[You returned an empty response. Respond to the user with what you know so far.]"
                ))
                continue

            if continuation_depth >= MAX_CONTINUATION_DEPTH:
                break
            if circuit_breaker_reason:
                break

            continuation_depth += 1
            logger.info(
                f"Max tool iterations reached ({total_tool_calls} total tool calls), "
                f"auto-continuing (depth {continuation_depth}/{MAX_CONTINUATION_DEPTH})"
            )
            if continuation_depth >= MAX_CONTINUATION_DEPTH:
                # Last chance — MUST respond or delegate
                nudge = (
                    f"[WRAP UP: You've made {total_tool_calls} tool calls. "
                    f"You MUST either respond to the user NOW with what you have, "
                    f"or spawn_actor() to delegate remaining work. No more solo exploration.]"
                )
            else:
                nudge = (
                    f"[You've made {total_tool_calls} tool calls. "
                    f"If more work is needed, consider spawning a subagent. "
                    f"Otherwise, respond to the user with your findings.]"
                )
            self.context.add_message(Message(role="user", content=nudge))

        # Hit continuation limit - request final response without tools
        if circuit_breaker_reason:
            logger.warning(f"Requesting final response after circuit breaker: {circuit_breaker_reason}")
        else:
            logger.warning("Max continuation depth reached, requesting final response")
        response = await self._call_api_no_tools()
        content = strip_model_tags(response["choices"][0]["message"].get("content", ""))
        
        if content:
            self.context.add_message(Message(role="assistant", content=content))
            return content
        
        return "Task processing limit reached. The work done so far has been saved."
    
    async def _get_api_kwargs(self) -> Dict:
        """Build kwargs for litellm API call, including OAuth if needed."""
        kwargs = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        # Custom API base for local/compatible providers
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
        # Anthropic Bearer auth via ANTHROPIC_AUTH_TOKEN (instead of x-api-key)
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if auth_token and "anthropic" in self.config.provider:
            kwargs["api_key"] = "placeholder"  # prevent litellm from erroring on missing key
            kwargs["extra_headers"] = {
                "Authorization": f"Bearer {auth_token}",
                "x-api-key": "",  # suppress default header
            }
        return kwargs
    
    async def _call_with_retry(self, kwargs: Dict, log_type: str, max_retries: int = 5) -> Dict:
        """Make API call with retry logic for transient errors."""
        import asyncio
        
        # OAuth path: route through direct Anthropic API
        if self._oauth:
            return await self._call_with_retry_oauth(kwargs, log_type, max_retries)
        
        last_error = None
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                result = response.model_dump()
                self._track_provider_headers(result)
                _log_llm_interaction(kwargs, result, log_type)
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                is_rate_limit = self._is_rate_limit_error(error_str)
                is_transient = self._is_transient_error(error_str)
                
                if is_rate_limit:
                    # Rate limit: wait longer, up to 60 seconds
                    wait_time = min(60, 15 * (attempt + 1))  # 15, 30, 45, 60, 60
                    logger.warning(f"Rate limit hit (attempt {attempt + 1}/{max_retries}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif is_transient:
                    # Transient error: shorter exponential backoff
                    wait_time = (attempt + 1) * 2  # 2, 4, 6, 8, 10 seconds
                    logger.warning(f"Transient error (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    # Non-retryable error, raise immediately
                    raise
        
        # All retries failed
        logger.error(f"API call failed after {max_retries} attempts: {last_error}")
        raise last_error
    
    async def _call_with_retry_oauth(self, kwargs: Dict, log_type: str, max_retries: int = 5) -> Dict:
        """Route any litellm-style kwargs through OAuth instead."""
        import asyncio
        
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        model = kwargs.get("model", self.config.model)
        max_tokens = kwargs.get("max_tokens", self.config.max_output_tokens)
        
        last_error = None
        for attempt in range(max_retries):
            try:
                result = await self._oauth.call_messages(
                    messages=messages,
                    tools=tools if tools else None,
                    model=model,
                    max_tokens=max_tokens,
                )
                self._track_provider_headers(result)
                _log_llm_interaction(kwargs, result, f"{log_type}_oauth")
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                is_rate_limit = self._is_rate_limit_error(error_str)
                is_transient = self._is_transient_error(error_str)
                
                if is_rate_limit:
                    wait_time = min(60, 15 * (attempt + 1))
                    logger.warning(f"OAuth rate limit (attempt {attempt + 1}/{max_retries}), waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif is_transient:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"OAuth transient error (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    raise
        
        logger.error(f"OAuth call failed after {max_retries} attempts: {last_error}")
        raise last_error

    @staticmethod
    def _is_rate_limit_error(error_str: str) -> bool:
        return any(x in error_str for x in ["rate_limit", "rate limit", "429", "too many requests"])

    @staticmethod
    def _is_transient_error(error_str: str) -> bool:
        transient_markers = [
            "timeout",
            "provider",
            "overloaded",
            "503",
            "502",
            "500",
            # Network / transport instability.
            "ssl",
            "tls",
            "bad record mac",
            "connection reset",
            "connection aborted",
            "network is unreachable",
            "temporarily unavailable",
            "eof occurred in violation of protocol",
        ]
        return any(x in error_str for x in transient_markers)
    
    async def _call_api(self, source: str = "") -> Dict:
        """Make API call via litellm (or OAuth if enabled)."""
        # Pre-flight: force compaction if context is too large
        self.context._compress_if_needed()
        messages = self.context.build_messages()
        
        # Notify console of context build
        token_count = self.context.count_tokens("".join(str(m) for m in messages))
        self._notify_context(messages, token_count)
        self._notify_status("thinking")
        
        logger.debug(f"API call: {len(messages)} messages, {len(self.tools)} tools")
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        if self.tools:
            tools = [t.copy() for t in self.tools]
            # Anthropic prompt caching: mark last tool for 1-hour caching
            is_anthropic = "claude" in self.config.model.lower() or "anthropic" in self.config.model.lower()
            if is_anthropic and tools:
                tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            kwargs["tools"] = tools

        debug_ts = None
        if LLM_DEBUG:
            try:
                debug_path = Path("logs/llm")
                debug_path.mkdir(parents=True, exist_ok=True)
                debug_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                (debug_path / f"{debug_ts}_request.json").write_text(
                    json.dumps(kwargs, indent=2, default=str)
                )
            except Exception as e:
                logger.warning(f"Failed to write debug request log: {e}")
        
        result = await self._call_with_retry(kwargs, "chat")
        self._track_usage(result, source=source or self._usage_scope, model=kwargs.get("model", self.config.model))

        if LLM_DEBUG and debug_ts:
            try:
                debug_path = Path("logs/llm")
                (debug_path / f"{debug_ts}_response.json").write_text(
                    json.dumps(result, indent=2, default=str)
                )
            except Exception as e:
                logger.warning(f"Failed to write debug response log: {e}")
        
        return result
    
    async def _call_api_no_tools(self, source: str = "") -> Dict:
        """Make API call without tools (for final response after hitting limit)."""
        messages = self.context.build_messages()
        
        logger.debug(f"API call (no tools): {len(messages)} messages")
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        result = await self._call_with_retry(kwargs, "chat_no_tools")
        self._track_usage(result, source=source or f"{self._usage_scope}:no_tools", model=kwargs.get("model", self.config.model))
        return result
    
    async def complete(self, prompt: str, use_aux: bool = False, usage_tag: str = "") -> str:
        """Simple completion without tools or context management.
        
        Used for summarization and other utility tasks.
        
        Args:
            prompt: The prompt to complete
            use_aux: If True, use auxiliary model (cheaper, for heartbeats/summarization)
            
        Returns:
            The completion text
        """
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = [{"role": "user", "content": prompt}]
        kwargs["temperature"] = 0.3
        kwargs["max_tokens"] = 2000
        
        # Use aux model if requested (for OAuth: same provider, just different model)
        if use_aux:
            kwargs["model"] = self.config.model_aux
        
        log_type = "complete" if not use_aux else "complete_aux"
        result = await self._call_with_retry(kwargs, log_type)
        source = usage_tag or (f"{self._usage_scope}:aux" if use_aux else f"{self._usage_scope}:complete")
        self._track_usage(result, source=source, model=kwargs.get("model", self.config.model))
        
        return result["choices"][0]["message"].get("content") or ""
    
    async def heartbeat(self, message: str) -> str:
        """Process heartbeat with minimal context and aux model.
        
        Uses lightweight system prompt, no conversation history, and aux model
        for cost efficiency. Tools are still available for checking tasks.
        
        Args:
            message: Heartbeat message
            
        Returns:
            Response string
        """
        # Build minimal messages (just system + heartbeat message)
        messages = [
            {"role": "system", "content": HEARTBEAT_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
        
        # Get task + file tools for heartbeat (reflection needs file access)
        task_tools = []
        heartbeat_keywords = ["todo", "task", "remind", "calendar", "read_file", "write_file", "edit_file", "list_directory", "grep_search"]
        for name, (func, schema) in self._tools.items():
            if any(kw in name.lower() for kw in heartbeat_keywords):
                task_tools.append({"type": "function", "function": schema})
        
        kwargs = {
            "model": self.config.model_aux,  # Use aux model
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 1000,
        }
        
        # Tools disabled for lightweight heartbeat — aux models (Gemini) don't use them reliably
        # Full context heartbeat (Kimi) has tools via the main chat() method
        
        # Simple loop for tool calls (max 5 iterations — read + write + reflect)
        for _ in range(5):
            result = await self._call_with_retry(kwargs, "heartbeat")
            self._track_usage(result, source=f"{self._usage_scope}:heartbeat", model=kwargs.get("model", self.config.model_aux))
            
            choice = result["choices"][0]
            message = choice["message"]
            tool_calls = message.get("tool_calls")
            
            # Check for tool calls
            if tool_calls:
                # Execute tools and add results
                kwargs["messages"].append(message)
                
                for tool_call in tool_calls:
                    func_name = tool_call["function"]["name"].strip()
                    try:
                        tool_args = json.loads(tool_call["function"]["arguments"])
                    except:
                        tool_args = {}
                    logger.info(f"Heartbeat tool: {func_name}({tool_args})")
                    func = self.get_tool(func_name)
                    
                    if func:
                        try:
                            import json, asyncio
                            args = json.loads(tool_call["function"]["arguments"])
                            if asyncio.iscoroutinefunction(func):
                                tool_result = await func(**args)
                            else:
                                tool_result = func(**args)
                        except Exception as e:
                            tool_result = f"Error: {e}"
                    else:
                        tool_result = f"Unknown tool: {func_name}"
                    
                    result_str = str(tool_result)[:2000]
                    logger.info(f"  Result: {result_str[:100]}...")
                    kwargs["messages"].append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result_str,
                    })
            else:
                # No tool calls, return response
                content = message.get("content") or "ok"
                logger.info(f"Heartbeat response (no tools): {content[:80]}...")
                return content
        
        return "ok"  # Max iterations reached
    
    async def close(self):
        """Cleanup (no-op with litellm, kept for API compatibility)."""
        pass
