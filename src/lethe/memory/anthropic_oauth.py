"""Anthropic OAuth client for Claude Max/Pro subscription auth.

Bypasses litellm to make direct API calls with Claude Code-compatible
headers, tool naming, and request format. Tokens are refreshed automatically.

Based on reverse-engineering from opencode-anthropic-auth plugin.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from lethe.paths import credentials_dir as _credentials_dir

logger = logging.getLogger(__name__)


class RateLimitError(RuntimeError):
    """Raised on HTTP 429. Carries retry_after seconds."""
    def __init__(self, message: str, retry_after: float = 30.0):
        super().__init__(message)
        self.retry_after = retry_after


# Claude Code's OAuth client ID (public, embedded in the CLI)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Endpoints
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"

# Claude Code billing attribution (extracted from CLI binary)
_CC_VERSION = "2.1.117"
_CC_SALT = "59cf53e54c78"


def _cc_version_hash(first_user_text: str) -> str:
    """Compute the 3-char hex suffix for cc_version, matching Claude Code's hL6().

    Claude Code picks characters at indices 4, 7, 20 of the first user message,
    concatenates with a salt and the build config object (which JS coerces to
    "[object Object]" inside a template literal), then SHA-256 truncates to 3 hex chars.
    """
    chars = "".join(first_user_text[i] if i < len(first_user_text) else "0" for i in (4, 7, 20))
    raw = f"{_CC_SALT}{chars}[object Object]"
    return hashlib.sha256(raw.encode()).hexdigest()[:3]


def _billing_header(first_user_text: str = "") -> str:
    """Build the billing attribution string embedded in the system prompt."""
    h = _cc_version_hash(first_user_text)
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "unknown")
    return f"x-anthropic-billing-header: cc_version={_CC_VERSION}.{h}; cc_entrypoint={entrypoint}; cch=00000;"

# Claude Code tool name mapping (our snake_case → Claude's PascalCase)
TOOL_NAME_TO_CLAUDE = {
    "bash": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "list_directory": "Glob",
    "grep_search": "Grep",
    "web_search": "WebSearch",
    "fetch_webpage": "WebFetch",
    "memory_read": "mcp__lethe__MemoryRead",
    "memory_update": "mcp__lethe__MemoryUpdate",
    "memory_append": "mcp__lethe__MemoryAppend",
    "archival_search": "mcp__lethe__ArchivalSearch",
    "archival_insert": "mcp__lethe__ArchivalInsert",
    "conversation_search": "mcp__lethe__ConversationSearch",
}

# Reverse mapping (Claude PascalCase → our snake_case)
TOOL_NAME_FROM_CLAUDE = {v: k for k, v in TOOL_NAME_TO_CLAUDE.items()}

# Model ID mapping (short → full dated ID)
MODEL_ID_MAP = {
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-opus-4-5": "claude-opus-4-5-20251101",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
}
MODEL_ID_REVERSE = {v: k for k, v in MODEL_ID_MAP.items()}

# Token file location
TOKEN_FILE = Path(
    os.environ.get("LETHE_ANTHROPIC_OAUTH_TOKENS")
    or str(_credentials_dir() / "anthropic_oauth_tokens.json")
)


def _to_pascal_case(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def _to_snake_case(name: str) -> str:
    """Convert PascalCase to snake_case."""
    import re
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    return s.lower()


def _map_tool_name_to_claude(name: str) -> str:
    """Map our tool name to Claude Code's expected format."""
    if name in TOOL_NAME_TO_CLAUDE:
        return TOOL_NAME_TO_CLAUDE[name]
    # Unknown tools: use mcp__lethe__PascalCase (Claude Code's MCP naming convention)
    return f"mcp__lethe__{_to_pascal_case(name)}"


def _map_tool_name_from_claude(name: str) -> str:
    """Map Claude Code's tool name back to ours."""
    if name in TOOL_NAME_FROM_CLAUDE:
        return TOOL_NAME_FROM_CLAUDE[name]
    # Strip mcp__<server>__ prefix and convert back
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return _to_snake_case(parts[2])
    if name.startswith("mcp_"):
        stripped = name[4:]
        return _to_snake_case(stripped)
    return _to_snake_case(name)


class AnthropicOAuth:
    """Direct Anthropic API client using OAuth tokens (Claude Max/Pro subscription).
    
    Handles:
    - Token storage, loading, and auto-refresh
    - Claude Code-compatible headers and request format
    - Tool name mapping (snake_case ↔ PascalCase)
    - Body normalization (remove temperature, tool_choice, cache_control)
    """
    
    def __init__(
        self,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[float] = None,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0
        self._client: Optional[httpx.AsyncClient] = None
        
        # Shared rate limit state: all callers check this before making requests
        self._rate_limit_until: float = 0  # monotonic time when rate limit expires
        self._rate_limit_lock = asyncio.Lock()
        
        # Try loading from env or file if not provided
        if not self.access_token:
            self._load_tokens()
    
    def _load_tokens(self):
        """Load tokens from env var or token file."""
        # Check env first (access token only — no refresh possible)
        env_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if env_token:
            self.access_token = env_token
            # No refresh token from env — token will fail when expired
            logger.info("OAuth: loaded access token from ANTHROPIC_AUTH_TOKEN env")
            return
        
        # Check token file
        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.expires_at = data.get("expires_at", 0)
                logger.info(f"OAuth: loaded tokens from {TOKEN_FILE}")
            except Exception as e:
                logger.error(f"OAuth: failed to load tokens from {TOKEN_FILE}: {e}")
    
    def save_tokens(self):
        """Persist tokens to file."""
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }, indent=2))
        # Restrict permissions (tokens are sensitive)
        TOKEN_FILE.chmod(0o600)
        logger.info(f"OAuth: saved tokens to {TOKEN_FILE}")
    
    @property
    def is_available(self) -> bool:
        """Check if OAuth is configured (has tokens)."""
        return bool(self.access_token)
    
    async def ensure_access(self):
        """Refresh the access token if expired."""
        if not self.refresh_token:
            # No refresh token (env-only mode) — just use what we have
            return
        
        # Refresh 60s before expiry
        if self.expires_at > time.time() + 60:
            return
        
        logger.info("OAuth: refreshing access token")
        client = await self._get_client()
        
        response = await client.post(TOKEN_URL, json={
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": CLIENT_ID,
        })
        
        if response.status_code != 200:
            raise RuntimeError(f"OAuth token refresh failed: {response.status_code} {response.text}")
        
        data = response.json()
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + data.get("expires_in", 3600)
        self.save_tokens()
        logger.info("OAuth: token refreshed successfully")
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=600.0)
        return self._client
    
    def _build_headers(self, has_tools: bool = True, is_stream: bool = False) -> dict:
        """Build Claude Code-compatible headers."""
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {self.access_token}",
            "anthropic-version": "2023-06-01",
            "user-agent": f"claude-cli/{_CC_VERSION} (external, cli)",
            "x-app": "cli",
            "anthropic-dangerous-direct-browser-access": "true",
            # Stainless headers (Claude SDK metadata)
            "x-stainless-arch": "x64",
            "x-stainless-lang": "js",
            "x-stainless-os": "Linux",
            "x-stainless-package-version": "0.70.0",
            "x-stainless-runtime": "node",
            "x-stainless-runtime-version": "v24.3.0",
            "x-stainless-retry-count": "0",
            "x-stainless-timeout": "600",
        }

        # Beta headers — claude-code-20250219 MUST always be present for OAuth to work
        betas = ["claude-code-20250219", "oauth-2025-04-20", "interleaved-thinking-2025-05-14"]
        headers["anthropic-beta"] = ",".join(betas)

        if is_stream:
            headers["x-stainless-helper-method"] = "stream"

        return headers
    
    def _normalize_model(self, model: str) -> str:
        """Map model to full dated ID if needed.
        
        Also strips litellm provider prefixes (e.g., 'anthropic/claude-...' → 'claude-...').
        """
        # Strip litellm provider prefix (anthropic/, openrouter/, etc.)
        if "/" in model:
            model = model.split("/", 1)[-1]
        return MODEL_ID_MAP.get(model, model)
    
    def _normalize_tools(self, tools: List[Dict]) -> List[Dict]:
        """Transform tool schemas to Claude Code format.
        
        - Rename tools to PascalCase
        - Keep input_schema params as snake_case (Claude Code uses snake_case)
        """
        normalized = []
        for tool in tools:
            t = tool.copy()
            if "function" in t:
                # litellm format: {"type": "function", "function": {"name": ..., "parameters": ...}}
                func = t["function"].copy()
                original_name = func.get("name", "")
                func["name"] = _map_tool_name_to_claude(original_name)
                # Anthropic native format uses input_schema, not parameters
                if "parameters" in func:
                    func["input_schema"] = func.pop("parameters")
                # Remove description sanitization issues
                normalized.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("input_schema", {"type": "object", "properties": {}}),
                })
            else:
                # Already in Anthropic native format
                t = t.copy()
                if "name" in t:
                    t["name"] = _map_tool_name_to_claude(t["name"])
                normalized.append(t)
        return normalized
    
    def _normalize_messages(self, messages: List[Dict]) -> tuple:
        """Convert litellm-format messages to Anthropic native format.
        
        Returns:
            (system_blocks, messages) - system extracted from messages
        """
        system_blocks = []
        api_messages = []
        
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            
            if role == "system":
                # Extract system prompt
                if isinstance(content, list):
                    # Structured system blocks — preserve cache_control for prompt caching
                    for block in content:
                        if isinstance(block, dict):
                            system_blocks.append(block)
                        else:
                            system_blocks.append({"type": "text", "text": str(block)})
                elif isinstance(content, str):
                    system_blocks.append({"type": "text", "text": content})
                continue
            
            if role == "tool":
                # Tool results → Anthropic format
                api_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": str(content),
                    }],
                })
                continue
            
            if role == "assistant":
                # Check for tool calls
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": str(content)})
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        name = _map_tool_name_to_claude(func.get("name", ""))
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": name,
                            "input": args,
                        })
                    api_messages.append({"role": "assistant", "content": blocks})
                else:
                    api_messages.append({"role": "assistant", "content": str(content)})
                continue
            
            # User messages
            if isinstance(content, list):
                # Multimodal content — convert image_url (OpenAI) to image (Anthropic)
                converted = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        img_data = block.get("image_url", {})
                        url = img_data.get("url", "") if isinstance(img_data, dict) else str(img_data)
                        if url.startswith("data:"):
                            # data:image/png;base64,<data>
                            try:
                                header, b64data = url.split(",", 1)
                                media_type = header.split(":")[1].split(";")[0]
                            except (ValueError, IndexError):
                                media_type = "image/png"
                                b64data = url
                            converted.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64data},
                            })
                        else:
                            # URL-based image
                            converted.append({
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            })
                    else:
                        converted.append(block)
                api_messages.append({"role": "user", "content": converted})
            else:
                api_messages.append({"role": "user", "content": str(content)})
        
        # Extract first user message text for billing header hash
        first_user_text = ""
        for msg in api_messages:
            if msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, str):
                    first_user_text = c
                elif isinstance(c, list):
                    for blk in c:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            first_user_text = blk.get("text", "")
                            break
                break

        # Prepend billing attribution and Claude Code identifier as system blocks.
        # The billing header is embedded in the system prompt (not an HTTP header) —
        # Claude Code's server reads it from the prompt text to classify billing.
        claude_code_prefix = "You are Claude Code, Anthropic's official CLI for Claude."
        system_blocks.insert(0, {"type": "text", "text": claude_code_prefix})
        system_blocks.insert(0, {"type": "text", "text": _billing_header(first_user_text)})
        
        # Merge consecutive same-role messages (Anthropic requires alternating roles)
        merged = []
        for msg in api_messages:
            if merged and merged[-1]["role"] == msg["role"]:
                # Merge content
                prev_content = merged[-1]["content"]
                new_content = msg["content"]
                
                if isinstance(prev_content, str) and isinstance(new_content, str):
                    merged[-1]["content"] = prev_content + "\n" + new_content
                elif isinstance(prev_content, list) and isinstance(new_content, list):
                    merged[-1]["content"] = prev_content + new_content
                elif isinstance(prev_content, str) and isinstance(new_content, list):
                    merged[-1]["content"] = [{"type": "text", "text": prev_content}] + new_content
                elif isinstance(prev_content, list) and isinstance(new_content, str):
                    merged[-1]["content"] = prev_content + [{"type": "text", "text": new_content}]
            else:
                merged.append(msg)
        
        # Anthropic requires the first message to be role=user
        if merged and merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": "[Continue]"})
        
        # Validate tool_result/tool_use pairing (Anthropic rejects orphans with 400)
        merged = self._clean_orphaned_tool_pairs(merged)

        # Strip trailing assistant messages — OAuth API doesn't support prefill
        while merged and merged[-1].get("role") == "assistant":
            merged.pop()

        return system_blocks, merged
    
    @staticmethod
    def _clean_orphaned_tool_pairs(messages: List[Dict]) -> List[Dict]:
        """Ensure every tool_use has a matching tool_result and vice versa.
        
        Anthropic requires:
        1. Every tool_result references a tool_use ID from the immediately preceding assistant message
        2. Every tool_use in an assistant message is followed by a tool_result in the next user message
        
        Violations cause 400 errors. This method strips orphans in both directions.
        """
        if not messages:
            return messages

        # Forward pass: collect tool_use IDs per assistant message
        # and check which ones have results in the next message
        tool_use_ids_by_idx = {}  # msg_idx → set of tool_use IDs
        tool_result_ids_by_idx = {}  # msg_idx → set of tool_use_ids referenced by tool_results

        for i, msg in enumerate(messages):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            uses = set()
            results = set()
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        uses.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        results.add(block.get("tool_use_id", ""))
            if uses:
                tool_use_ids_by_idx[i] = uses
            if results:
                tool_result_ids_by_idx[i] = results

        # Build set of tool_use IDs that have matching results
        matched_use_ids = set()
        for i, use_ids in tool_use_ids_by_idx.items():
            # Look for results in subsequent messages until next assistant tool_use
            for j in range(i + 1, len(messages)):
                if j in tool_result_ids_by_idx:
                    matched_use_ids.update(use_ids & tool_result_ids_by_idx[j])
                # Stop looking if we hit another assistant message with tool_use
                if j in tool_use_ids_by_idx:
                    break

        # Build set of tool_result IDs that have matching uses
        matched_result_ids = set()
        for i, result_ids in tool_result_ids_by_idx.items():
            # Look backwards for the tool_use
            for j in range(i - 1, -1, -1):
                if j in tool_use_ids_by_idx:
                    matched_result_ids.update(result_ids & tool_use_ids_by_idx[j])
                    break

        # Clean pass: filter out unmatched tool_use and tool_result blocks
        cleaned = []
        for i, msg in enumerate(messages):
            content = msg.get("content")
            if not isinstance(content, list):
                cleaned.append(msg)
                continue

            filtered = []
            for block in content:
                if not isinstance(block, dict):
                    filtered.append(block)
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tid = block.get("id", "")
                    if tid in matched_use_ids:
                        filtered.append(block)
                    else:
                        logger.warning(f"Dropping orphaned tool_use (id={tid[:30]})")
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid in matched_result_ids:
                        filtered.append(block)
                    else:
                        logger.warning(f"Dropping orphaned tool_result (tool_use_id={tid[:30]})")
                else:
                    filtered.append(block)

            if filtered:
                cleaned.append({**msg, "content": filtered})
            else:
                # Entire message was orphaned tool blocks — check if we can skip it
                # Don't drop if it would create consecutive same-role messages
                logger.warning(f"Dropping message {i} (all tool blocks orphaned)")

        return cleaned
    
    def _parse_response(self, data: dict) -> dict:
        """Convert Anthropic native response to litellm-compatible format.
        
        Maps tool names back and restructures to OpenAI-compatible format
        that the rest of Lethe expects.
        """
        # Extract content blocks
        content_blocks = data.get("content", [])
        
        text_parts = []
        tool_calls = []
        
        for block in content_blocks:
            block_type = block.get("type", "")
            
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            
            elif block_type == "tool_use":
                claude_name = block.get("name", "")
                our_name = _map_tool_name_from_claude(claude_name)
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": our_name,
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        
        # Build litellm-compatible response
        message = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        
        # Map model ID back
        model = data.get("model", "")
        model = MODEL_ID_REVERSE.get(model, model)
        
        return {
            "id": data.get("id", ""),
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": _map_stop_reason(data.get("stop_reason", "end_turn")),
            }],
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": (
                    data.get("usage", {}).get("input_tokens", 0) +
                    data.get("usage", {}).get("output_tokens", 0)
                ),
                # Pass through cache stats if present
                "cache_creation_input_tokens": data.get("usage", {}).get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": data.get("usage", {}).get("cache_read_input_tokens", 0),
            },
        }
    
    async def call_messages(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        model: str = "claude-opus-4-6",
        max_tokens: int = 8000,
        **kwargs,
    ) -> dict:
        """Make a Claude API call with OAuth auth.
        
        Args:
            messages: litellm-format messages (system/user/assistant/tool)
            tools: litellm-format tool schemas (optional)
            model: model name
            max_tokens: max output tokens
            
        Returns:
            litellm-compatible response dict
        """
        await self.ensure_access()
        
        # Normalize
        model = self._normalize_model(model)
        system_blocks, api_messages = self._normalize_messages(messages)
        
        has_tools = bool(tools)
        api_tools = self._normalize_tools(tools) if tools else []
        
        # Build request body
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": api_messages,
            "tools": api_tools,  # Always send (even empty — Claude Code does)
        }
        
        # Inject metadata.user_id if available
        user_id = _get_metadata_user_id()
        if user_id:
            body["metadata"] = {"user_id": user_id}
        
        # Note: temperature and tool_choice intentionally NOT included
        # (Claude Code doesn't send them for OAuth)
        
        # Build headers
        headers = self._build_headers(has_tools=has_tools)
        
        # Make request
        url = f"{MESSAGES_URL}?beta=true"
        client = await self._get_client()
        
        # Guard: Anthropic requires at least one non-system message
        if not api_messages:
            logger.warning("OAuth: no messages after normalization (system-only), adding placeholder")
            api_messages = [{"role": "user", "content": "[Continue]"}]
            body["messages"] = api_messages
        
        logger.info(f"OAuth API call: model={model}, messages={len(api_messages)}, tools={len(api_tools)}")
        
        # Wait for any active rate limit cooldown before making the request
        await self._wait_for_rate_limit()
        
        response = await client.post(url, headers=headers, json=body)
        
        if response.status_code == 429:
            self._set_rate_limit(response)
            error_text = response.text[:500]
            logger.warning(f"OAuth rate limited (429): {error_text}")
            raise RateLimitError(
                f"Anthropic OAuth rate limited (429) - {error_text}",
                retry_after=self._parse_retry_after(response),
            )
        
        if response.status_code == 529:
            error_text = response.text[:500]
            logger.warning(f"OAuth overloaded (529): {error_text}")
            raise RuntimeError(
                f"Anthropic OAuth overloaded (529) - {error_text}"
            )
        
        if response.status_code != 200:
            error_text = response.text[:500]
            logger.error(f"OAuth API error: {response.status_code} {error_text}")
            raise RuntimeError(
                f"Anthropic OAuth API error: {response.status_code} - {error_text}"
            )
        
        data = response.json()
        usage = data.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        cache_str = f", cache r/w={cache_read}/{cache_write}" if cache_read or cache_write else ""
        logger.info(f"OAuth API response: {in_tok} in + {out_tok} out tokens{cache_str}")

        parsed = self._parse_response(data)
        parsed["_response_headers"] = {k.lower(): v for k, v in response.headers.items()}
        return parsed
    
    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        """Parse retry-after header, return seconds to wait."""
        retry_after = response.headers.get("retry-after", "")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return 30.0  # Default if no header
    
    def _set_rate_limit(self, response: httpx.Response):
        """Set shared rate limit cooldown from response headers."""
        wait = self._parse_retry_after(response)
        self._rate_limit_until = time.monotonic() + wait
        logger.warning(f"Rate limit set: waiting {wait:.0f}s (until +{wait:.0f}s)")
    
    async def _wait_for_rate_limit(self):
        """Wait if a rate limit cooldown is active. All callers share this."""
        remaining = self._rate_limit_until - time.monotonic()
        if remaining > 0:
            logger.info(f"Rate limit active, waiting {remaining:.1f}s before request")
            await asyncio.sleep(remaining)
    
    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def _map_stop_reason(reason: str) -> str:
    """Map Anthropic stop_reason to OpenAI finish_reason."""
    return {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
    }.get(reason, reason)


def _get_metadata_user_id() -> Optional[str]:
    """Get user_id from ~/.claude.json (Claude Code config)."""
    home = os.environ.get("HOME", os.environ.get("USERPROFILE", ""))
    if not home:
        return None
    
    claude_config = Path(home) / ".claude.json"
    if not claude_config.exists():
        return None
    
    try:
        data = json.loads(claude_config.read_text())
        user_id = data.get("userID")
        account_uuid = data.get("oauthAccount", {}).get("accountUuid")
        
        # Find a session ID
        session_id = None
        projects = data.get("projects", {})
        for project in projects.values():
            if isinstance(project, dict) and project.get("lastSessionId"):
                session_id = project["lastSessionId"]
                break
        
        if user_id and account_uuid and session_id:
            return f"user_{user_id}_account_{account_uuid}_session_{session_id}"
    except Exception:
        pass
    
    return None


def is_oauth_available() -> bool:
    """Check if OAuth tokens are available (env or file)."""
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            return bool(data.get("access_token"))
        except Exception:
            pass
    return False
