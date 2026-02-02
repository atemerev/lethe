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

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging
litellm.suppress_debug_info = True

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

# Minimal heartbeat system prompt (lightweight, no full identity)
HEARTBEAT_SYSTEM_PROMPT = """You are a background task checker. Your job is to:
1. Check pending tasks, reminders, or calendar items if tools are available
2. Report anything time-sensitive that needs user attention

Be concise. End with either:
- "ok" if nothing urgent
- A brief message if something needs attention NOW"""

# Letta-style summarization prompt
SUMMARIZE_PROMPT = """Your job is to summarize a history of previous messages in a conversation between an AI persona and a human.
The conversation you are given is from a fixed context window and may not be complete.
Messages sent by the AI are marked with the 'assistant' role.
The AI can also make calls to tools, whose outputs can be seen in messages with the 'tool' role.
Messages the user sends are in the 'user' role.
The 'user' role is also used for important system events, such as heartbeat events.
Summarize what happened in the conversation from the perspective of the AI (use first person).
Keep your summary less than 100 words, do NOT exceed this word limit.
Only output the summary, do NOT include anything else in your output."""


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
    name: Optional[str] = None  # for tool messages
    tool_call_id: Optional[str] = None  # for tool results
    tool_calls: Optional[List[Dict]] = None  # for assistant tool calls
    
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
    _summarizer: Optional[Callable] = None  # Set by LLMClient
    
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
            messages: List of dicts with 'role', 'content', and optionally 'created_at' keys
        
        Note: Tool messages are filtered out - they require paired tool_use/tool_result
        which can't be guaranteed from history. Only user/assistant messages loaded.
        """
        for msg in messages:
            role = msg.get("role", "user")
            
            # Skip tool messages - they can't be properly paired from history
            # Anthropic requires tool_result to immediately follow tool_use
            if role == "tool":
                continue
            
            content = msg.get("content", "")
            # Prepend timestamp if available
            if msg.get("created_at"):
                timestamp = msg["created_at"][:16].replace("T", " ")  # "2026-02-02 10:30"
                content = f"[{timestamp}] {content}"
            
            # Skip assistant messages that were just tool calls (no text content)
            if role == "assistant" and not content:
                continue
            
            self.messages.append(Message(
                role=role,
                content=content,
            ))
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
                # Clip summary to reasonable length (Letta uses 50k chars default)
                if len(new_summary) > 50000:
                    new_summary = new_summary[:50000] + "\n[Summary truncated...]"
                self.summary = new_summary
                logger.info(f"Summary updated: {len(self.summary)} chars")
        else:
            # Fallback: text-based summary
            old_text = "\n".join(f"{m.role}: {m.get_text_content()[:300]}" for m in to_summarize[-10:])
            if self.summary:
                self.summary = f"{self.summary}\n\n[Additional context from {len(to_summarize)} messages]\n{old_text}"
            else:
                self.summary = f"[Summary of {len(to_summarize)} previous messages]\n{old_text}"
            # Clip fallback summary too
            if len(self.summary) > 50000:
                self.summary = self.summary[:50000] + "\n[Summary truncated...]"
    
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
            "summary_chars": len(self.summary),
            "compaction_threshold": f"{COMPACTION_TRIGGER_RATIO * 100:.0f}%",
            "keep_ratio": f"{SLIDING_WINDOW_KEEP_RATIO * 100:.0f}%",
        }
    
    def build_messages(self) -> List[Dict]:
        """Build messages array for API call."""
        # Combine system prompt with memory context and summary
        system_content = f"""{self.system_prompt}

<memory>
{self.memory_context}
</memory>
"""
        
        if self.summary:
            system_content += f"""
<conversation_summary>
{self.summary}
</conversation_summary>
"""
        
        system_content += f"\nCurrent time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        
        messages = [{"role": "system", "content": system_content}]
        
        for msg in self.messages:
            m = {"role": msg.role, "content": msg.content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            messages.append(m)
        
        return messages


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
    ):
        self.config = config or LLMConfig()
        self.context = ContextWindow(
            system_prompt=system_prompt,
            memory_context=memory_context,
            config=self.config,
        )
        
        # Tools: name -> (function, schema)
        self._tools: Dict[str, tuple[Callable, Dict]] = {}
        
        # No httpx client needed - using litellm
        
        # Set up summarizer callback
        self.context._summarizer = self._summarize_messages_sync
        
        logger.info(f"AsyncLLMClient initialized with model {self.config.model}")
    
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
        
        for iteration in range(max_tool_iterations):
            # Make API call
            response = await self._call_api()
            
            # Check for tool calls
            choice = response["choices"][0]
            assistant_msg = choice["message"]
            
            # Get content (might be present even with tool calls)
            content = assistant_msg.get("content") or ""
            
            # Handle tool calls
            tool_calls = assistant_msg.get("tool_calls")
            
            # Callback with intermediate message (only when there are tool calls - i.e. more work to do)
            # Don't callback with final response - that's returned and handled by caller
            if content and on_message and tool_calls:
                await on_message(content)
            if tool_calls:
                # Add assistant message with tool calls
                self.context.add_message(Message(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                ))
                
                # Execute tools and add results
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"].strip()  # Strip whitespace (model quirk)
                    tool_args = json.loads(tool_call["function"]["arguments"])
                    tool_id = tool_call["id"]
                    
                    logger.info(f"Executing tool: {tool_name}({list(tool_args.keys())})")
                    
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
                    
                    # Check for image attachment in result
                    if isinstance(result, dict) and "_image_attachment" in result:
                        img = result["_image_attachment"]
                        if on_image and img.get("path"):
                            await on_image(img["path"])
                        # Remove attachment from result for context
                        result_for_context = {k: v for k, v in result.items() if k != "_image_attachment"}
                        result = result_for_context
                    
                    # Add tool result
                    self.context.add_message(Message(
                        role="tool",
                        content=str(result),
                        tool_call_id=tool_id,
                    ))
                
                continue  # Loop to get next response
            
            # No tool calls - we have final response
            self.context.add_message(Message(role="assistant", content=content))
            return content
        
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
        content = response["choices"][0]["message"].get("content", "")
        
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
    
    async def _call_api(self) -> Dict:
        """Make API call via litellm."""
        messages = self.context.build_messages()
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        if self.tools:
            kwargs["tools"] = self.tools
        
        logger.debug(f"API call: {len(messages)} messages, {len(self.tools)} tools")
        
        response = await acompletion(**kwargs)
        result = response.model_dump()
        
        # Debug logging
        _log_llm_interaction(kwargs, result, "chat")
        
        return result
    
    async def _call_api_no_tools(self) -> Dict:
        """Make API call without tools (for final response after hitting limit)."""
        messages = self.context.build_messages()
        
        kwargs = await self._get_api_kwargs()
        kwargs["messages"] = messages
        
        logger.debug(f"API call (no tools): {len(messages)} messages")
        
        response = await acompletion(**kwargs)
        result = response.model_dump()
        
        _log_llm_interaction(kwargs, result, "chat_no_tools")
        
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
        
        response = await acompletion(**kwargs)
        result = response.model_dump()
        
        _log_llm_interaction(kwargs, result, "complete" if not use_aux else "complete_aux")
        
        return response.choices[0].message.content or ""
    
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
        
        # Get only task-related tools (if any)
        task_tools = []
        for name, (func, schema) in self._tools.items():
            if any(kw in name.lower() for kw in ["todo", "task", "remind", "calendar"]):
                task_tools.append({"type": "function", "function": schema})
        
        kwargs = {
            "model": self.config.model_aux,  # Use aux model
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 1000,
        }
        
        if task_tools:
            kwargs["tools"] = task_tools
        
        # Simple loop for tool calls (max 3 iterations)
        for _ in range(3):
            response = await acompletion(**kwargs)
            result = response.model_dump()
            _log_llm_interaction(kwargs, result, "heartbeat")
            
            choice = response.choices[0]
            
            # Check for tool calls
            if choice.message.tool_calls:
                # Execute tools and add results
                kwargs["messages"].append(choice.message.model_dump())
                
                for tool_call in choice.message.tool_calls:
                    func_name = tool_call.function.name.strip()
                    func = self.get_tool(func_name)
                    
                    if func:
                        try:
                            import json
                            args = json.loads(tool_call.function.arguments)
                            if asyncio.iscoroutinefunction(func):
                                tool_result = await func(**args)
                            else:
                                tool_result = func(**args)
                        except Exception as e:
                            tool_result = f"Error: {e}"
                    else:
                        tool_result = f"Unknown tool: {func_name}"
                    
                    kwargs["messages"].append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(tool_result)[:2000],
                    })
            else:
                # No tool calls, return response
                return choice.message.content or "ok"
        
        return "ok"  # Max iterations reached
    
    async def close(self):
        """Cleanup (no-op with litellm, kept for API compatibility)."""
        pass
