"""Direct LLM client with context management.

Replaces Letta's agent loop with direct OpenRouter API calls.
Handles context preparation, token counting, and tool calling.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable
import logging
import httpx

logger = logging.getLogger(__name__)

# OpenRouter API
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2.5"  # Kimi K2.5

# Context limits (chars, roughly 4 chars per token)
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_LIMIT = 128000  # tokens
DEFAULT_MAX_OUTPUT = 8000  # tokens

# Context budget management (Letta-style)
TOKEN_SAFETY_MARGIN = 1.3  # Safety margin for approximate token counting
SLIDING_WINDOW_KEEP_RATIO = 0.7  # Keep 70% of context after compaction
COMPACTION_TRIGGER_RATIO = 0.85  # Trigger compaction at 85% capacity

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
    """LLM configuration."""
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    base_url: str = OPENROUTER_BASE_URL
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    max_output_tokens: int = DEFAULT_MAX_OUTPUT
    temperature: float = 0.7
    
    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not set")


@dataclass
class Message:
    """A conversation message."""
    role: str  # system, user, assistant, tool
    content: str
    name: Optional[str] = None  # for tool messages
    tool_call_id: Optional[str] = None  # for tool results
    tool_calls: Optional[List[Dict]] = None  # for assistant tool calls


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
            messages: List of dicts with 'role' and 'content' keys
        """
        for msg in messages:
            self.messages.append(Message(
                role=msg.get("role", "user"),
                content=msg.get("content", ""),
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
        total = sum(self.count_tokens(m.content) for m in self.messages)
        
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
            old_text = "\n".join(f"{m.role}: {m.content[:300]}" for m in to_summarize[-10:])
            if self.summary:
                self.summary = f"{self.summary}\n\n[Additional context from {len(to_summarize)} messages]\n{old_text}"
            else:
                self.summary = f"[Summary of {len(to_summarize)} previous messages]\n{old_text}"
            # Clip fallback summary too
            if len(self.summary) > 50000:
                self.summary = self.summary[:50000] + "\n[Summary truncated...]"
    
    def get_stats(self) -> dict:
        """Get context window statistics."""
        message_tokens = sum(self.count_tokens(m.content) for m in self.messages)
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


class LLMClient:
    """Direct LLM client using OpenRouter.
    
    Handles:
    - Context window management
    - Tool/function calling
    - Streaming responses
    """
    
    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        system_prompt: str = "",
        memory_context: str = "",
        tools: Optional[List[Dict]] = None,
    ):
        self.config = config or LLMConfig()
        self.context = ContextWindow(
            system_prompt=system_prompt,
            memory_context=memory_context,
            config=self.config,
        )
        self.tools = tools or []
        self.tool_handlers: Dict[str, Callable] = {}
        
        self.client = httpx.Client(
            base_url=self.config.base_url,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/atemerev/lethe",
                "X-Title": "Lethe AI Assistant",
            },
            timeout=120.0,
        )
        
        # Set up summarizer callback
        self.context._summarizer = self._summarize_messages
        
        logger.info(f"LLMClient initialized with model {self.config.model}")
    
    def _summarize_messages(self, messages: List[Message], existing_summary: str) -> str:
        """Summarize a list of messages using LLM."""
        # Format messages for summarization
        formatted = []
        for m in messages:
            formatted.append(f"{m.role}: {m.content}")
        conversation = "\n".join(formatted)
        
        if existing_summary:
            conversation = f"Previous summary: {existing_summary}\n\nNew messages to incorporate:\n{conversation}"
        
        # Call LLM for summarization
        try:
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": conversation},
                ],
                "temperature": 0.3,  # Lower temperature for factual summary
                "max_tokens": 500,
            }
            
            response = self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return existing_summary  # Keep old summary on failure
    
    def register_tool(self, name: str, handler: Callable, schema: Dict):
        """Register a tool with its handler and schema."""
        self.tool_handlers[name] = handler
        self.tools.append({
            "type": "function",
            "function": schema,
        })
    
    def update_memory_context(self, memory_context: str):
        """Update the memory context (e.g., after memory block changes)."""
        self.context.memory_context = memory_context
    
    def chat(
        self,
        message: str,
        max_tool_iterations: int = 10,
    ) -> str:
        """Send a message and get response, handling tool calls.
        
        Args:
            message: User message
            max_tool_iterations: Max tool call loops
            
        Returns:
            Final assistant response text
        """
        # Add user message
        self.context.add_message(Message(role="user", content=message))
        
        for iteration in range(max_tool_iterations):
            # Make API call
            response = self._call_api()
            
            # Check for tool calls
            choice = response["choices"][0]
            assistant_msg = choice["message"]
            
            # Handle tool calls
            tool_calls = assistant_msg.get("tool_calls")
            if tool_calls:
                # Add assistant message with tool calls
                self.context.add_message(Message(
                    role="assistant",
                    content=assistant_msg.get("content") or "",
                    tool_calls=tool_calls,
                ))
                
                # Execute tools and add results
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"].strip()  # Strip whitespace
                    tool_args = json.loads(tool_call["function"]["arguments"])
                    tool_id = tool_call["id"]
                    
                    logger.info(f"Executing tool: {tool_name}({tool_args})")
                    
                    # Execute tool
                    if tool_name in self.tool_handlers:
                        try:
                            result = self.tool_handlers[tool_name](**tool_args)
                        except Exception as e:
                            result = f"Error: {e}"
                            logger.error(f"Tool {tool_name} failed: {e}")
                    else:
                        result = f"Unknown tool: {tool_name}"
                    
                    # Add tool result
                    self.context.add_message(Message(
                        role="tool",
                        content=str(result),
                        tool_call_id=tool_id,
                    ))
                
                continue  # Loop to get next response
            
            # No tool calls - we have final response
            content = assistant_msg.get("content") or ""
            self.context.add_message(Message(role="assistant", content=content))
            return content
        
        return "Max tool iterations reached"
    
    def _call_api(self) -> Dict:
        """Make API call to OpenRouter."""
        messages = self.context.build_messages()
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        
        if self.tools:
            payload["tools"] = self.tools
        
        logger.debug(f"API call: {len(messages)} messages, {len(self.tools)} tools")
        
        response = self.client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()
    
    def get_context_stats(self) -> Dict:
        """Get context window statistics."""
        return self.context.get_stats()


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
        
        self._client: Optional[httpx.AsyncClient] = None
        
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
            self._tools[func.__name__] = (func, schema)
    
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
            formatted.append(f"{m.role}: {m.content}")
        conversation = "\n".join(formatted)
        
        if existing_summary:
            conversation = f"Previous summary: {existing_summary}\n\nNew messages to incorporate:\n{conversation}"
        
        # Use sync client for summarization
        try:
            with httpx.Client(
                base_url=self.config.base_url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            ) as client:
                payload = {
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": SUMMARIZE_PROMPT},
                        {"role": "user", "content": conversation},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                }
                
                response = client.post("/chat/completions", json=payload)
                response.raise_for_status()
                result = response.json()
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return existing_summary
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/atemerev/lethe",
                    "X-Title": "Lethe AI Assistant",
                },
                timeout=120.0,
            )
        return self._client
    
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
            messages: List of dicts with 'role' and 'content' keys
        """
        self.context.load_messages(messages)
    
    async def chat(
        self,
        message: str,
        max_tool_iterations: int = 10,
        on_message: Optional[Callable] = None,
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
            
            # Callback with intermediate message
            if content and on_message:
                await on_message(content)
            
            # Handle tool calls
            tool_calls = assistant_msg.get("tool_calls")
            if tool_calls:
                # Add assistant message with tool calls
                self.context.add_message(Message(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                ))
                
                # Execute tools and add results
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    tool_args = json.loads(tool_call["function"]["arguments"])
                    tool_id = tool_call["id"]
                    
                    logger.info(f"Executing tool: {tool_name}({list(tool_args.keys())})")
                    
                    # Notify about tool execution
                    if on_message:
                        # Format tool call for user visibility
                        args_preview = ", ".join(f"{k}={repr(v)[:30]}" for k, v in list(tool_args.items())[:3])
                        await on_message(f"🔧 {tool_name}({args_preview})")
                    
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
    
    async def _call_api(self) -> Dict:
        """Make API call to OpenRouter."""
        client = await self._get_client()
        messages = self.context.build_messages()
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        
        if self.tools:
            payload["tools"] = self.tools
        
        logger.debug(f"API call: {len(messages)} messages, {len(self.tools)} tools")
        
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def _call_api_no_tools(self) -> Dict:
        """Make API call without tools (for final response after hitting limit)."""
        client = await self._get_client()
        messages = self.context.build_messages()
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        # Deliberately not including tools
        
        logger.debug(f"API call (no tools): {len(messages)} messages")
        
        response = await client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
