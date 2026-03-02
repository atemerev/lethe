"""
OAuth authentication for Anthropic API.

Handles token refresh and expiry. Falls back to Claude Code CLI credentials.
Includes Telegram-based recovery flow for when refresh tokens are invalidated.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# OAuth configuration
ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_AUTH_URL = "https://console.anthropic.com/oauth/authorize"
ANTHROPIC_API_BASE = "https://api.anthropic.com"
CLIENT_ID = "9d1c250a-e61b-44b0-b9ee-f6e0c65408e3"  # Claude Code CLI client ID
REDIRECT_URI = "http://localhost:65532/oauth/callback"


@dataclass
class OAuthTokens:
    """OAuth token container."""

    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        # Refresh 60 seconds before expiry
        return time.time() >= self.expires_at - 60

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthTokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
        )


def get_claude_code_tokens() -> Optional[OAuthTokens]:
    """Load tokens from Claude Code CLI credentials file."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None

    try:
        with open(creds_path) as f:
            data = json.load(f)

        # Claude Code stores tokens with a key based on scopes
        for key, value in data.items():
            if isinstance(value, dict) and "accessToken" in value:
                return OAuthTokens(
                    access_token=value["accessToken"],
                    refresh_token=value["refreshToken"],
                    expires_at=value["expiresAt"] / 1000,  # Convert ms to seconds
                )
    except Exception as e:
        logger.warning(f"Failed to load Claude Code tokens: {e}")

    return None


class OAuthClient:
    """OAuth client for Anthropic API with token management and Telegram recovery."""

    def __init__(self, tokens_path: Optional[Path] = None, telegram_bot=None, chat_id: Optional[int] = None):
        self.tokens_path = tokens_path or Path.home() / ".lethe" / "claude_tokens.json"
        self._tokens: Optional[OAuthTokens] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._refresh_lock = asyncio.Lock()
        
        # Telegram recovery support
        self._telegram_bot = telegram_bot
        self._chat_id = chat_id
        self._pending_auth_code: Optional[str] = None
        self._auth_code_event: Optional[asyncio.Event] = None

    def set_telegram(self, telegram_bot, chat_id: int):
        """Set Telegram bot and chat ID for recovery flow."""
        self._telegram_bot = telegram_bot
        self._chat_id = chat_id

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=120.0)
        return self._http_client

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def _load_tokens(self) -> Optional[OAuthTokens]:
        """Load tokens from file."""
        if self.tokens_path.exists():
            try:
                with open(self.tokens_path) as f:
                    data = json.load(f)
                return OAuthTokens.from_dict(data)
            except Exception as e:
                logger.warning(f"Failed to load tokens: {e}")
        return None

    def _save_tokens(self, tokens: OAuthTokens):
        """Save tokens to file."""
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.tokens_path, "w") as f:
            json.dump(tokens.to_dict(), f)

    def _generate_pkce(self) -> tuple[str, str]:
        """Generate PKCE code verifier and challenge."""
        # Generate random code verifier (43-128 chars, base64url-safe)
        code_verifier = secrets.token_urlsafe(32)
        
        # Generate code challenge (SHA256 hash, base64url-encoded)
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
        
        return code_verifier, code_challenge

    def _build_auth_url(self, code_challenge: str) -> str:
        """Build OAuth authorization URL with PKCE."""
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "user:inference",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{ANTHROPIC_AUTH_URL}?{query}"

    async def _exchange_code_for_tokens(self, code: str, code_verifier: str) -> OAuthTokens:
        """Exchange authorization code for tokens."""
        response = await self.http_client.post(
            ANTHROPIC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        
        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.status_code} {response.text}")
        
        data = response.json()
        tokens = OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + data.get("expires_in", 3600),
        )
        self._save_tokens(tokens)
        return tokens

    async def receive_auth_code(self, code: str):
        """Receive authorization code from Telegram message handler."""
        self._pending_auth_code = code
        if self._auth_code_event:
            self._auth_code_event.set()

    async def _telegram_oauth_recovery(self) -> Optional[OAuthTokens]:
        """Attempt to recover OAuth via Telegram interaction."""
        if not self._telegram_bot or not self._chat_id:
            logger.warning("Telegram recovery not available: bot or chat_id not set")
            return None

        try:
            # Generate PKCE challenge
            code_verifier, code_challenge = self._generate_pkce()
            auth_url = self._build_auth_url(code_challenge)
            
            # Send auth URL to user
            message = (
                "🔐 **OAuth token expired!**\n\n"
                "Please click this link to re-authorize:\n"
                f"{auth_url}\n\n"
                "After authorizing, you'll be redirected to localhost (which won't work). "
                "Copy the `code=` parameter from the URL and send it to me.\n\n"
                "Example: if the URL is `http://localhost:65532/oauth/callback?code=ABC123`, "
                "just send: `ABC123`"
            )
            
            await self._telegram_bot.send_message(self._chat_id, message, parse_mode="Markdown")
            logger.info("Sent OAuth recovery message via Telegram")
            
            # Wait for user to send auth code (with timeout)
            self._auth_code_event = asyncio.Event()
            self._pending_auth_code = None
            
            try:
                await asyncio.wait_for(self._auth_code_event.wait(), timeout=300)  # 5 minute timeout
            except asyncio.TimeoutError:
                await self._telegram_bot.send_message(
                    self._chat_id, 
                    "⏰ OAuth recovery timed out. Please restart me or try again.",
                    parse_mode="Markdown"
                )
                return None
            
            if not self._pending_auth_code:
                return None
            
            # Exchange code for tokens
            code = self._pending_auth_code.strip()
            self._pending_auth_code = None
            
            tokens = await self._exchange_code_for_tokens(code, code_verifier)
            self._tokens = tokens
            
            await self._telegram_bot.send_message(
                self._chat_id,
                "✅ OAuth tokens refreshed successfully! Resuming normal operation.",
                parse_mode="Markdown"
            )
            
            logger.info("OAuth recovery via Telegram successful")
            return tokens
            
        except Exception as e:
            logger.error(f"Telegram OAuth recovery failed: {e}")
            if self._telegram_bot and self._chat_id:
                await self._telegram_bot.send_message(
                    self._chat_id,
                    f"❌ OAuth recovery failed: {e}",
                    parse_mode="Markdown"
                )
            return None

    async def _refresh_tokens(self) -> bool:
        """Refresh the access token using refresh token."""
        if not self._tokens:
            return False

        async with self._refresh_lock:
            # Double-check after acquiring lock
            if self._tokens and not self._tokens.needs_refresh:
                return True

            try:
                response = await self.http_client.post(
                    ANTHROPIC_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": self._tokens.refresh_token,
                    },
                )

                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"Token refresh failed: {response.status_code} {error_text}")
                    
                    # Check if it's an invalid_grant error
                    if "invalid_grant" in error_text.lower():
                        # Try falling back to CLI tokens with a different refresh token
                        claude_tokens = get_claude_code_tokens()
                        if claude_tokens and claude_tokens.refresh_token != self._tokens.refresh_token:
                            logger.info("Falling back to Claude CLI tokens")
                            self._tokens = claude_tokens
                            self._save_tokens(claude_tokens)
                            return await self._refresh_tokens()  # Retry with CLI tokens
                        
                        # CLI tokens didn't help, try Telegram recovery
                        logger.info("Attempting Telegram OAuth recovery...")
                        recovered_tokens = await self._telegram_oauth_recovery()
                        if recovered_tokens:
                            return True
                    
                    # All recovery attempts failed
                    self._tokens = None
                    if self.tokens_path.exists():
                        self.tokens_path.unlink()
                    return False

                data = response.json()
                self._tokens = OAuthTokens(
                    access_token=data["access_token"],
                    # Use new refresh token if provided, otherwise keep existing
                    refresh_token=data.get("refresh_token", self._tokens.refresh_token),
                    expires_at=time.time() + data.get("expires_in", 3600),
                )
                self._save_tokens(self._tokens)
                logger.info(f"OAuth tokens refreshed, expires at {time.ctime(self._tokens.expires_at)}")
                return True

            except Exception as e:
                logger.error(f"Token refresh error: {e}")
                return False

    async def ensure_access(self) -> Optional[str]:
        """Ensure we have a valid access token, refreshing if needed."""
        # Load tokens if not in memory
        if not self._tokens:
            self._tokens = self._load_tokens()

        # Try Claude Code CLI tokens if we don't have any
        if not self._tokens:
            self._tokens = get_claude_code_tokens()
            if self._tokens:
                self._save_tokens(self._tokens)
                logger.info("Loaded tokens from Claude Code CLI")

        if not self._tokens:
            # No tokens anywhere - try Telegram recovery as last resort
            logger.warning("No tokens available, attempting Telegram OAuth recovery...")
            recovered_tokens = await self._telegram_oauth_recovery()
            if not recovered_tokens:
                raise ValueError(
                    "No OAuth tokens available. Run 'claude' CLI to authenticate, "
                    "or send an auth code via Telegram."
                )

        # Refresh if needed
        if self._tokens.needs_refresh:
            logger.info("OAuth: refreshing access token")
            if not await self._refresh_tokens():
                raise ValueError("OAuth token refresh failed")

        return self._tokens.access_token

    async def api_request(
        self,
        method: str,
        endpoint: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Make an authenticated API request."""
        access_token = await self.ensure_access()

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"
        headers["anthropic-version"] = "2023-06-01"

        url = f"{ANTHROPIC_API_BASE}{endpoint}"
        response = await self.http_client.request(method, url, headers=headers, **kwargs)

        if response.status_code == 401:
            # Token might have been invalidated, try refresh
            logger.warning("Got 401, attempting token refresh")
            if await self._refresh_tokens():
                access_token = self._tokens.access_token
                headers["Authorization"] = f"Bearer {access_token}"
                response = await self.http_client.request(
                    method, url, headers=headers, **kwargs
                )

        response.raise_for_status()
        return response.json()

    async def stream_request(
        self,
        endpoint: str,
        **kwargs,
    ):
        """Make an authenticated streaming API request."""
        access_token = await self.ensure_access()

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"
        headers["anthropic-version"] = "2023-06-01"

        url = f"{ANTHROPIC_API_BASE}{endpoint}"

        async with self.http_client.stream("POST", url, headers=headers, **kwargs) as response:
            if response.status_code == 401:
                # Can't easily retry a stream, but try to refresh for next time
                await self._refresh_tokens()
                raise httpx.HTTPStatusError(
                    "Authentication failed", request=response.request, response=response
                )

            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line
