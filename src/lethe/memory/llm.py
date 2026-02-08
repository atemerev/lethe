"""Direct LLM client with context management.

Uses litellm for multi-provider support (OpenRouter, Anthropic, OpenAI, etc).
Handles context preparation, token counting, and tool calling.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Union
import logging

import litellm
from litellm import acompletion, completion

from lethe.utils import strip_model_tags

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
        "default_model_aux": "openrouter/moonshotai/kimi-k2.5-0127",
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

# Minimal heartbeat system prompt (lightweight, no full identity)
HEARTBEAT_SYSTEM_PROMPT = """You are Lethe's background reflection process. Your job is to:
1. Check pending tasks, reminders, or calendar items
2. Reflect on your principal's goals and well-being
3. Update ~/lethe/questions.md with new reflections or answered questions
4. Report anything time-sensitive that needs user attention

You have file tools — use them to read and update ~/lethe/questions.md.

Be concise. End with either:
- "ok" if nothing urgent
- A brief message if something needs attention NOW"""

# Letta-style summarization prompt
SUMMARIZE_PROMPT = """Summarize this conversation concisely from the AI's perspective (first person).

Focus on:
- Key decisions and outcomes
- Important facts learned about the user
- Unresolved tasks or questions

Keep it under 100 words. Be terse - bullet points are fine.
Output ONLY the summary, nothing else."""


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
        
        # Verify API key exists
        env_key = provider_config.get("env_key")
        if env_key and not os.environ.get(env_key):
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
            return self.created_at.strftime("%Y-%m-%d %H:%M")
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
    _summarizer: Optional[Callable] = None  # Set by LLMClient
    _tool_reference: str = ""  # Compact tool list for system prompt (non-Anthropic models)
    
    def count_tokens(self, text: str) -> int:
        """Approximate token count with safety margin.
        
        Uses 4 chars per token approximation with 1.3x safety margin
        to avoid underestimating (Letta's approach).
        """
        base_count = len(text) // CHARS_PER_TOKEN
        return int(base_count * TOKEN_SAFETY_MARGIN)
    
    def get_fixed_tokens(self) -> int:
        """Get tokens used by fixed content."""
        return (
            self.count_tokens(self.system_prompt) +
            self.count_tokens(self.memory_context) +
            self.count_tokens(self.summary)
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
            
            # Skip tool messages from history — tool_use/tool_result pairing
            # only works within the same session. Different models use different
            # ID formats (Kimi: "functions.name:N", Anthropic: "toolu_xxx")
            if role == "tool":
                skipped_tool += 1
                continue
            
            # Skip assistant messages that are only tool_calls (no text)
            if role == "assistant" and metadata.get("tool_calls") and not content:
                skipped_tool += 1
                continue
            
            # Strip tool_calls from assistant messages loaded from history
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
        1. Trigger when messages exceed COMPACTION_TRIGGER_RATIO (85%) of available
        2. Find cutoff point to keep SLIDING_WINDOW_KEEP_RATIO (70%) of messages
        3. Cutoff must be at assistant message boundary (avoid mid-conversation splits)
        4. Summarize messages before cutoff, keep messages after
        """
        available = self.get_available_tokens()
        total = sum(self.count_tokens(m.get_text_content()) for m in self.messages)
        
        # Check if compaction needed
        if total <= available * COMPACTION_TRIGGER_RATIO or len(self.messages) <= 4:
            return
        
        logger.info(f"Context compaction triggered: {total} tokens > {available * COMPACTION_TRIGGER_RATIO:.0f} threshold")
        
        # Calculate target: keep SLIDING_WINDOW_KEEP_RATIO of messages
        target_keep = int(len(self.messages) * SLIDING_WINDOW_KEEP_RATIO)
        target_keep = max(target_keep, 2)  # Keep at least 2 messages
        
        # Find cutoff point - must end on assistant message for clean break
        cutoff = len(self.messages) - target_keep
        
        # Adjust cutoff to end on assistant message boundary
        while cutoff > 0 and cutoff < len(self.messages):
            if self.messages[cutoff - 1].role == "assistant":
                break
            cutoff -= 1
        
        if cutoff <= 0:
            # Can't find good cutoff, use simple split
            cutoff = len(self.messages) - target_keep
        
        to_summarize = self.messages[:cutoff]
        self.messages = self.messages[cutoff:]
        
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
        
        # Build system as array of content blocks for Anthropic prompt caching
        # Static content (cacheable): system prompt + memory blocks
        # Dynamic content (not cached): summary
        
        system_content = []
        
        # 1. Static block: System prompt + memory blocks (CACHED)
        # These rarely change, so we cache them together
        static_parts = [self.system_prompt]
        if self.memory_context:
            static_parts.append(self.memory_context)
        
        static_text = "\n".join(static_parts)
        
        # For non-Anthropic models: embed tool reference directly in system prompt
        # (Kimi K2.5 works much better with tools visible in context text)
        if self._tool_reference:
            static_text += "\n\n" + self._tool_reference
        
        system_content.append({
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"}  # Cache for 5 minutes
        })
        
        # 2. Dynamic block: Conversation summary (NOT cached - changes frequently)
        if self.summary:
            system_content.append({
                "type": "text",
                "text": f"\n<conversation_summary>\n{self.summary}\n</conversation_summary>"
            })
        
        messages = [{"role": "system", "content": system_content}]
        
        # Find indices of image messages (to keep only most recent)
        image_indices = []
        for i, msg in enumerate(self.messages):
            if isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        image_indices.append(i)
                        break
        
        # Keep only last 2 images, replace older ones with placeholders
        old_image_indices = set(image_indices[:-2]) if len(image_indices) > 2 else set()
        
        # Find heartbeat messages (keep only the last one)
        heartbeat_indices = []
        for i, msg in enumerate(self.messages):
            if msg.role == "user" and isinstance(msg.content, str):
                if msg.content.startswith("[System Heartbeat"):
                    heartbeat_indices.append(i)
        # Skip all but the last heartbeat (and its response)
        old_heartbeat_indices = set(heartbeat_indices[:-1]) if len(heartbeat_indices) > 1 else set()
        
        # Find the last tool_call group (assistant with tool_calls + its tool results)
        # Only the last group gets full content; older tool results are truncated to metadata
        last_tool_assistant_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role == "assistant" and self.messages[i].tool_calls:
                last_tool_assistant_idx = i
                break
        
        # Collect tool_call_ids from the last tool group
        last_tool_ids = set()
        if last_tool_assistant_idx is not None:
            for tc in self.messages[last_tool_assistant_idx].tool_calls:
                last_tool_ids.add(tc.get("id", ""))
        
        # Build message array with timestamps on user messages only
        # (Assistant sees when user said things, but doesn't mimic timestamp format)
        skip_next_assistant = False
        for i, msg in enumerate(self.messages):
            # Skip old heartbeats and their responses
            if i in old_heartbeat_indices:
                skip_next_assistant = True
                continue
            if skip_next_assistant and msg.role == "assistant":
                skip_next_assistant = False
                continue
            skip_next_assistant = False
            
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
            
            if msg.role == "user" and not msg.tool_calls and isinstance(content, str):
                timestamp = msg.format_timestamp()
                if timestamp:
                    content = f"[{timestamp}] {content}"
            
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
        
        # Sanitize tool IDs for Anthropic (must match ^[a-zA-Z0-9-]+$)
        # Kimi needs exact format: functions.func_name:idx — DO NOT sanitize
        if "claude" in self.config.model.lower() or "anthropic" in self.config.model.lower():
            import re
            for m in merged:
                if m.get("tool_call_id"):
                    m["tool_call_id"] = re.sub(r'[^a-zA-Z0-9-]', '-', m["tool_call_id"])
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        if "id" in tc:
                            tc["id"] = re.sub(r'[^a-zA-Z0-9-]', '-', tc["id"])
        
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
        
        # Set up summarizer callback
        self.context._summarizer = self._summarize_messages_sync
        
        logger.info(f"AsyncLLMClient initialized with model {self.config.model}")
    
    def _add_and_persist(self, message: "Message"):
        """Add message to context and persist to storage."""
        self.context.add_message(message)
        
        if self._on_message_persist:
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
    
    def add_tools(self, tools: List[tuple[Callable, Dict]]):
        """Add multiple tools as (function, schema) tuples."""
        for func, schema in tools:
            # Use schema name as key (allows name overrides for async imports)
            name = schema.get("name", func.__name__)
            self._tools[name] = (func, schema)
    
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
                model=self.config.model,
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
    
    def _track_usage(self, result: Dict):
        """Track token usage from API response."""
        if hasattr(self, "_on_token_usage") and self._on_token_usage:
            usage = result.get("usage", {})
            total = usage.get("total_tokens", 0)
            if total:
                self._on_token_usage(total)
    
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
        _continuation_depth: int = 0,  # Internal: track auto-continue depth
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
        
        MAX_CONTINUATION_DEPTH = 3  # Max auto-continues (total iterations = 3 * 10 = 30)
        
        # Add user message
        self.context.add_message(Message(role="user", content=message))
        
        empty_count = 0  # Track consecutive empty responses
        for iteration in range(max_tool_iterations):
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
                for tc in tool_calls:
                    # Generate ID if missing (some models omit it)
                    if not tc.get("id"):
                        tc["id"] = f"call-{uuid.uuid4().hex[:12]}"
            
            # Callback with intermediate message (only when there are tool calls - i.e. more work to do)
            # Don't callback with final response - that's returned and handled by caller
            if content and on_message and tool_calls:
                await on_message(strip_model_tags(content))
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
                
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"].strip()  # Strip whitespace (model quirk)
                    tool_id = tool_call["id"]
                    
                    # Parse tool arguments (may be malformed JSON from some models)
                    try:
                        tool_args = json.loads(tool_call["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        logger.error(f"Tool {tool_name} has malformed JSON args: {e}")
                        # Add error result and continue (and persist)
                        self._add_and_persist(Message(
                            role="tool",
                            content=f"Error: malformed tool arguments - {e}",
                            tool_call_id=tool_id,
                        ))
                        continue
                    
                    logger.info(f"Executing tool: {tool_name}({list(tool_args.keys())})")
                    self._notify_status("tool_call", tool_name)
                    
                    # Execute tool (handle both sync and async)
                    handler = self.get_tool(tool_name)
                    if handler:
                        try:
                            if asyncio.iscoroutinefunction(handler):
                                result = await handler(**tool_args)
                            else:
                                result = handler(**tool_args)
                        except Exception as e:
                            result = f"Error: {e}"
                            logger.error(f"Tool {tool_name} failed: {e}")
                    else:
                        result = f"Unknown tool: {tool_name}"
                    
                    logger.info(f"  Result: {str(result)[:100]}...")
                    
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
                    ))
                
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
            
            logger.info("Empty response, nudging model to act")
            self.context.add_message(Message(
                role="user",
                content="[Continue — use your tools or respond to the user.]"
            ))
            continue
        
        # Max iterations reached - continue with another batch automatically
        if _continuation_depth < MAX_CONTINUATION_DEPTH:
            logger.info(f"Max tool iterations reached, auto-continuing (depth {_continuation_depth + 1}/{MAX_CONTINUATION_DEPTH})")
            
            # Recurse with fresh iteration count
            return await self.chat(
                message="[Continue with your task]",
                max_tool_iterations=max_tool_iterations,
                on_message=on_message,
                on_image=on_image,
                _continuation_depth=_continuation_depth + 1,
            )
        
        # Hit continuation limit - request final response without tools
        logger.warning(f"Max continuation depth reached, requesting final response")
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
        return kwargs
    
    async def _call_with_retry(self, kwargs: Dict, log_type: str, max_retries: int = 5) -> Dict:
        """Make API call with retry logic for transient errors."""
        import asyncio
        
        last_error = None
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                result = response.model_dump()
                _log_llm_interaction(kwargs, result, log_type)
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Check if it's a rate limit error (needs longer wait)
                is_rate_limit = any(x in error_str for x in ["rate_limit", "rate limit", "429", "too many requests"])
                
                # Check if it's a transient error (shorter wait)
                is_transient = any(x in error_str for x in ["timeout", "provider", "overloaded", "503", "502", "500"])
                
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
    
    async def _call_api(self) -> Dict:
        """Make API call via litellm."""
        messages = self.context.build_messages()
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        if self.tools:
            kwargs["tools"] = self.tools
        
        # Notify console of context build
        token_count = self.context.count_tokens("".join(str(m) for m in messages))
        self._notify_context(messages, token_count)
        self._notify_status("thinking")
        
        logger.debug(f"API call: {len(messages)} messages, {len(self.tools)} tools")
        
        # Dump full context for debugging
        debug_path = Path("logs/llm")
        debug_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        (debug_path / f"{ts}_request.json").write_text(
            json.dumps(kwargs, indent=2, default=str)
        )
        
        result = await self._call_with_retry(kwargs, "chat")
        self._track_usage(result)
        
        (debug_path / f"{ts}_response.json").write_text(
            json.dumps(result, indent=2, default=str)
        )
        
        return result
    
    async def _call_api_no_tools(self) -> Dict:
        """Make API call without tools (for final response after hitting limit)."""
        messages = self.context.build_messages()
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        logger.debug(f"API call (no tools): {len(messages)} messages")
        
        result = await self._call_with_retry(kwargs, "chat_no_tools")
        self._track_usage(result)
        return result
    
    async def complete(self, prompt: str, use_aux: bool = False) -> str:
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
        kwargs["temperature"] = 0.3  # Lower temperature for factual tasks
        kwargs["max_tokens"] = 2000
        
        # Use aux model if requested
        if use_aux:
            kwargs["model"] = self.config.model_aux
        
        log_type = "complete" if not use_aux else "complete_aux"
        result = await self._call_with_retry(kwargs, log_type)
        
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
        
        if task_tools:
            kwargs["tools"] = task_tools
            logger.info(f"Heartbeat: {len(task_tools)} tools available: {[t['function']['name'] for t in task_tools]}")
        else:
            logger.warning("Heartbeat: NO tools available!")
        
        # Simple loop for tool calls (max 5 iterations — read + write + reflect)
        for _ in range(5):
            result = await self._call_with_retry(kwargs, "heartbeat")
            
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
                            import json
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
