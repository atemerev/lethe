"""OpenAI Codex OAuth client for ChatGPT Plus/Pro subscription auth.

Uses ChatGPT OAuth tokens to call the Codex Responses endpoint directly.
Tokens are refreshed automatically when a refresh token is available.
"""

import base64
import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

# OpenAI Codex OAuth client ID (public, used by Codex-compatible CLIs)
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Endpoints
ISSUER = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER}/oauth/token"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_INSTRUCTIONS = "You are Lethe, a helpful and precise assistant."

# Token file location
TOKEN_FILE = Path(
    os.environ.get("LETHE_OPENAI_OAUTH_TOKENS", "~/.lethe/openai_oauth_tokens.json")
).expanduser()

# Common account-id claim paths
JWT_ACCOUNT_PATH = "https://api.openai.com/auth"


def _decode_base64url(value: str) -> bytes:
    """Decode URL-safe base64 with optional missing padding."""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _parse_jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    """Decode JWT payload claims without signature verification."""
    if not token or token.count(".") != 2:
        return None
    try:
        _, payload_b64, _ = token.split(".", 2)
        payload = _decode_base64url(payload_b64).decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _extract_account_id_from_claims(claims: Dict[str, Any]) -> Optional[str]:
    """Extract ChatGPT account/org id from JWT claims."""
    direct = claims.get("chatgpt_account_id")
    if isinstance(direct, str) and direct:
        return direct

    nested = claims.get(JWT_ACCOUNT_PATH)
    if isinstance(nested, dict):
        nested_id = nested.get("chatgpt_account_id")
        if isinstance(nested_id, str) and nested_id:
            return nested_id

    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id

    return None


def _extract_account_id(tokens: Dict[str, Any]) -> Optional[str]:
    """Extract account id from token response (id_token preferred)."""
    id_token = tokens.get("id_token")
    if isinstance(id_token, str) and id_token:
        claims = _parse_jwt_claims(id_token)
        if claims:
            account_id = _extract_account_id_from_claims(claims)
            if account_id:
                return account_id

    access_token = tokens.get("access_token")
    if isinstance(access_token, str) and access_token:
        claims = _parse_jwt_claims(access_token)
        if claims:
            return _extract_account_id_from_claims(claims)

    return None


def extract_account_id_from_tokens(tokens: Dict[str, Any]) -> Optional[str]:
    """Public helper for account-id extraction from OAuth token payloads."""
    return _extract_account_id(tokens)


def _map_finish_reason(reason: str) -> Optional[str]:
    """Map OpenAI Responses stop_reason to chat.completion finish_reason."""
    return {
        "stop": "stop",
        "tool_call": "tool_calls",
        "tool_calls": "tool_calls",
        "max_output_tokens": "length",
        "length": "length",
        "content_filter": "content_filter",
    }.get(reason, reason or None)


class OpenAIOAuth:
    """Direct OpenAI Codex Responses client using OAuth tokens."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        expires_at: Optional[float] = None,
        account_id: Optional[str] = None,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0
        self.account_id = account_id
        self._client: Optional[httpx.AsyncClient] = None

        if not self.access_token:
            self._load_tokens()

    def _load_tokens(self):
        """Load tokens from env var or token file."""
        env_token = os.environ.get("OPENAI_AUTH_TOKEN")
        if env_token:
            self.access_token = env_token
            self.account_id = self.account_id or _extract_account_id({"access_token": env_token})
            logger.info("OpenAI OAuth: loaded access token from OPENAI_AUTH_TOKEN env")
            return

        if TOKEN_FILE.exists():
            try:
                data = json.loads(TOKEN_FILE.read_text())
                self.access_token = data.get("access_token") or data.get("access")
                self.refresh_token = data.get("refresh_token") or data.get("refresh")
                self.account_id = data.get("account_id") or data.get("accountId")

                raw_exp = data.get("expires_at", data.get("expires", 0))
                if isinstance(raw_exp, (int, float)):
                    # Some tools store ms epoch.
                    self.expires_at = raw_exp / 1000 if raw_exp > 10_000_000_000 else raw_exp
                else:
                    self.expires_at = 0

                if self.access_token and not self.account_id:
                    self.account_id = _extract_account_id({"access_token": self.access_token})

                logger.info(f"OpenAI OAuth: loaded tokens from {TOKEN_FILE}")
            except Exception as e:
                logger.error(f"OpenAI OAuth: failed to load tokens from {TOKEN_FILE}: {e}")

    def save_tokens(self):
        """Persist tokens to file."""
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at,
                    "account_id": self.account_id,
                },
                indent=2,
            )
        )
        TOKEN_FILE.chmod(0o600)
        logger.info(f"OpenAI OAuth: saved tokens to {TOKEN_FILE}")

    @property
    def is_available(self) -> bool:
        """Check if OAuth is configured (has access token)."""
        return bool(self.access_token)

    async def ensure_access(self):
        """Refresh access token if expired and refresh token is available."""
        if not self.refresh_token:
            return

        # Refresh 60 seconds before expiry.
        if self.expires_at > time.time() + 60:
            return

        logger.info("OpenAI OAuth: refreshing access token")
        client = await self._get_client()
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": CLIENT_ID,
        }
        response = await client.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI OAuth token refresh failed: {response.status_code} {response.text}"
            )

        data = response.json()
        access = data.get("access_token")
        if not access:
            raise RuntimeError(f"OpenAI OAuth refresh missing access_token: {data}")

        self.access_token = access
        # Keep previous refresh token if response omits it.
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + float(data.get("expires_in", 3600))
        self.account_id = _extract_account_id(data) or self.account_id
        self.save_tokens()
        logger.info("OpenAI OAuth: token refreshed successfully")

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=600.0)
        return self._client

    def _build_user_agent(self) -> str:
        """Build OpenClaw/pi-ai compatible identity string."""
        sys_name = (platform.system() or "unknown").lower()
        release = platform.release() or "unknown"
        arch = (platform.machine() or "unknown").lower()
        return f"pi ({sys_name} {release}; {arch})"

    def _build_headers(self) -> Dict[str, str]:
        """Build OpenAI Codex OAuth headers."""
        headers = {
            "authorization": f"Bearer {self.access_token}",
            "OpenAI-Beta": "responses=experimental",
            "originator": "pi",
            "user-agent": self._build_user_agent(),
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        return headers

    def _normalize_model(self, model: str) -> str:
        """Normalize model id and strip provider prefixes."""
        if "/" in model:
            model = model.split("/", 1)[-1]
        unsupported_chatgpt_codex_models = {"gpt-5.2-mini", "gpt-5-mini"}
        if model in unsupported_chatgpt_codex_models:
            logger.warning(
                "OpenAI OAuth/Codex model %s is unsupported for ChatGPT accounts; "
                "falling back to gpt-5.2",
                model,
            )
        model_map = {
            "gpt-5-codex": "gpt-5.3-codex",
            "gpt-5": "gpt-5.2",
            # ChatGPT-account Codex OAuth does not currently accept mini variants.
            "gpt-5.2-mini": "gpt-5.2",
            "gpt-5-mini": "gpt-5.2",
        }
        return model_map.get(model, model)

    @staticmethod
    def _json_string(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value if value is not None else {})
        except Exception:
            return "{}"

    @staticmethod
    def _text_from_system_content(content: Any) -> str:
        """Extract plain text from system content that may be structured blocks."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
                elif block:
                    chunks.append(str(block))
            return "\n".join(chunks)
        if content is None:
            return ""
        return str(content)

    @staticmethod
    def _extract_image_url(image_value: Any) -> Optional[str]:
        """Extract image URL string from multimodal block payloads."""
        if isinstance(image_value, str) and image_value:
            return image_value
        if isinstance(image_value, dict):
            url = image_value.get("url")
            if isinstance(url, str) and url:
                return url
        return None

    def _normalize_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Lethe/litellm chat messages into OpenAI Responses input items."""
        input_items: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_text = self._text_from_system_content(content)
                if system_text:
                    input_items.append({"role": "system", "content": system_text})
                continue

            if role == "assistant":
                if content:
                    input_items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": str(content)}],
                        }
                    )
                for tc in msg.get("tool_calls", []) or []:
                    func = tc.get("function", {}) or {}
                    name = str(func.get("name", "") or "")
                    if not name:
                        continue
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id") or f"call_{uuid4().hex[:12]}",
                            "name": name,
                            "arguments": self._json_string(func.get("arguments", {})),
                        }
                    )
                continue

            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id", ""),
                        "output": self._json_string(content),
                    }
                )
                continue

            # Default to user role.
            if isinstance(content, str):
                input_items.append(
                    {"role": "user", "content": [{"type": "input_text", "text": content}]}
                )
            elif isinstance(content, list):
                parts: List[Dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            parts.append({"type": "input_text", "text": str(text)})
                    elif block_type == "image_url":
                        image_url = self._extract_image_url(block.get("image_url"))
                        if image_url:
                            parts.append({"type": "input_image", "image_url": image_url})
                if parts:
                    input_items.append({"role": "user", "content": parts})
            elif content is not None:
                input_items.append(
                    {"role": "user", "content": [{"type": "input_text", "text": str(content)}]}
                )

        return input_items

    def _extract_instructions(self, messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
        """Extract system prompt text for Responses.instructions.

        Returns instructions text and messages with system-role entries removed.
        """
        instructions_parts: List[str] = []
        non_system_messages: List[Dict[str, Any]] = []

        for msg in messages:
            if msg.get("role") == "system":
                text = self._text_from_system_content(msg.get("content", ""))
                if text:
                    text = text.strip()
                    if text:
                        instructions_parts.append(text)
                continue
            non_system_messages.append(msg)

        return "\n\n".join(instructions_parts).strip(), non_system_messages

    def _normalize_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert litellm-style tools into OpenAI Responses tool schema."""
        normalized: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") != "function":
                continue
            func = tool.get("function", {}) or {}
            name = str(func.get("name", "") or "")
            if not name:
                continue
            item: Dict[str, Any] = {
                "type": "function",
                "name": name,
                "description": str(func.get("description", "") or ""),
                "parameters": func.get("parameters", {"type": "object", "properties": {}}),
            }
            if "strict" in func:
                item["strict"] = func["strict"]
            normalized.append(item)
        return normalized

    @staticmethod
    def _iter_sse_events(raw: str) -> List[tuple[str, str]]:
        """Parse SSE event/data blocks from a non-streamed response body."""
        events: List[tuple[str, str]] = []
        event_name: Optional[str] = None
        data_lines: List[str] = []

        for line in raw.splitlines():
            if line.startswith("event: "):
                if event_name is not None:
                    events.append((event_name, "\n".join(data_lines)))
                event_name = line[len("event: ") :].strip()
                data_lines = []
                continue
            if line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
                continue
            if not line.strip() and event_name is not None:
                events.append((event_name, "\n".join(data_lines)))
                event_name = None
                data_lines = []

        if event_name is not None:
            events.append((event_name, "\n".join(data_lines)))
        return events

    def _parse_streamed_response(self, raw: str) -> Dict[str, Any]:
        """Parse Codex SSE response text and return a response object payload."""
        latest_response: Optional[Dict[str, Any]] = None
        for event_name, data in self._iter_sse_events(raw):
            if not data:
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            response = payload.get("response")
            if isinstance(response, dict):
                latest_response = response
                if event_name == "response.completed":
                    return response

        if latest_response is not None:
            return latest_response
        raise RuntimeError("OpenAI OAuth API error: could not parse streamed response payload")

    def _parse_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert OpenAI Responses payload to chat.completion-like shape."""
        response = data.get("response", data)
        output = response.get("output", []) or []

        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        for item in output:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type", "")
            if item_type == "message":
                for content in item.get("content", []) or []:
                    if (
                        isinstance(content, dict)
                        and content.get("type") == "output_text"
                        and isinstance(content.get("text"), str)
                    ):
                        text_parts.append(content["text"])
            elif item_type == "function_call":
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = self._json_string(arguments)
                tool_calls.append(
                    {
                        "id": item.get("id") or item.get("call_id") or f"toolu_{uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }
                )

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage_raw = response.get("usage", {}) or {}
        prompt_tokens = int(usage_raw.get("input_tokens", 0) or 0)
        completion_tokens = int(usage_raw.get("output_tokens", 0) or 0)
        total_tokens = usage_raw.get("total_tokens")
        if not isinstance(total_tokens, int):
            total_tokens = prompt_tokens + completion_tokens

        usage: Dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        input_details = usage_raw.get("input_tokens_details")
        if isinstance(input_details, dict):
            cached = input_details.get("cached_tokens")
            if isinstance(cached, (int, float)):
                usage["prompt_tokens_details"] = {"cached_tokens": int(cached)}

        output_details = usage_raw.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = output_details.get("reasoning_tokens")
            if isinstance(reasoning_tokens, (int, float)):
                usage["completion_tokens_details"] = {
                    "reasoning_tokens": int(reasoning_tokens)
                }

        model = str(response.get("model", ""))
        stop_reason = str(response.get("stop_reason", "stop"))
        finish_reason = _map_finish_reason(stop_reason)

        return {
            "id": str(response.get("id", f"chatcmpl_{uuid4().hex[:16]}")),
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    async def call_messages(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: str = "gpt-5.3-codex",
        max_tokens: int = 8000,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Call OpenAI Codex Responses endpoint using OAuth access token."""
        await self.ensure_access()

        model_id = self._normalize_model(model)
        explicit_instructions = kwargs.get("instructions")
        system_instructions, non_system_messages = self._extract_instructions(messages)
        if isinstance(explicit_instructions, str) and explicit_instructions.strip():
            instructions = explicit_instructions.strip()
        elif system_instructions:
            instructions = system_instructions
        else:
            instructions = DEFAULT_INSTRUCTIONS

        input_items = self._normalize_messages(non_system_messages)
        if not input_items:
            input_items = [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "[Continue]"}],
                }
            ]

        payload: Dict[str, Any] = {
            "model": model_id,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if max_tokens and max_tokens != 8000:
            # chatgpt.com/backend-api/codex/responses currently rejects max token params.
            logger.debug(
                "OpenAI OAuth/Codex endpoint ignores max token controls; requested max_tokens=%s",
                max_tokens,
            )
        if tools:
            payload["tools"] = self._normalize_tools(tools)

        headers = self._build_headers()
        client = await self._get_client()
        logger.info(
            "OpenAI OAuth API call: model=%s, input_items=%s, tools=%s",
            model_id,
            len(input_items),
            len(payload.get("tools", [])),
        )
        response = await client.post(RESPONSES_URL, headers=headers, json=payload)

        if response.status_code != 200:
            error_text = response.text[:500]
            logger.error(f"OpenAI OAuth API error: {response.status_code} {error_text}")
            raise RuntimeError(
                f"OpenAI OAuth API error: {response.status_code} - {error_text}"
            )

        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            data = response.json()
        else:
            data = self._parse_streamed_response(response.text)
        parsed = self._parse_response(data)
        parsed["_response_headers"] = {k.lower(): v for k, v in response.headers.items()}
        return parsed

    async def close(self):
        """Close the shared HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def is_oauth_available_openai() -> bool:
    """Check if OpenAI OAuth access tokens are available (env or file)."""
    if os.environ.get("OPENAI_AUTH_TOKEN"):
        return True
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            return bool(data.get("access_token") or data.get("access"))
        except Exception:
            pass
    return False
