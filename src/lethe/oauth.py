"""OAuth authentication for Claude Max subscription.

Handles PKCE OAuth flow for Claude Max/Pro subscriptions.
Stores tokens and handles automatic refresh.

For headless/server deployments, sends auth URL via Telegram and
user pastes the redirect URL back.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable, Awaitable
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

# Claude OAuth endpoints
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/api/oauth/token"

# Claude Code CLI client_id (public)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Redirect URI - uses localhost but user will copy the URL manually for remote setups
REDIRECT_URI = "http://localhost:19532/callback"

# Token storage
DEFAULT_TOKEN_PATH = Path("~/.config/lethe/claude_tokens.json").expanduser()


@dataclass
class OAuthTokens:
    """OAuth token storage."""
    access_token: str
    refresh_token: str
    expires_at: datetime
    
    def is_expired(self) -> bool:
        """Check if access token is expired (with 5 min buffer)."""
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(minutes=5)
    
    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "OAuthTokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    # Generate random code verifier (43-128 chars, URL-safe)
    verifier = secrets.token_urlsafe(32)
    
    # Create challenge = base64url(sha256(verifier))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    
    return verifier, challenge


class ClaudeOAuth:
    """Handles Claude Max OAuth authentication.
    
    Supports two modes:
    1. Interactive (console): Opens browser, runs local callback server
    2. Telegram: Sends auth URL via Telegram, user pastes redirect URL back
    """
    
    def __init__(
        self,
        token_path: Optional[Path] = None,
        send_message: Optional[Callable[[str], Awaitable[None]]] = None,
        receive_message: Optional[Callable[[], Awaitable[str]]] = None,
    ):
        """Initialize OAuth handler.
        
        Args:
            token_path: Path to store tokens
            send_message: Async function to send messages (e.g., via Telegram)
            receive_message: Async function to receive user input (e.g., via Telegram)
        """
        self.token_path = token_path or DEFAULT_TOKEN_PATH
        self._tokens: Optional[OAuthTokens] = None
        self._send_message = send_message
        self._receive_message = receive_message
        self._pending_auth: Optional[dict] = None  # Stores verifier/state during auth
        self._load_tokens()
    
    def _load_tokens(self):
        """Load tokens from disk if available."""
        if self.token_path.exists():
            try:
                data = json.loads(self.token_path.read_text())
                self._tokens = OAuthTokens.from_dict(data)
                logger.info("Loaded existing Claude OAuth tokens")
            except Exception as e:
                logger.warning(f"Failed to load tokens: {e}")
    
    def _save_tokens(self):
        """Save tokens to disk."""
        if self._tokens:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(json.dumps(self._tokens.to_dict(), indent=2))
            # Secure file permissions
            os.chmod(self.token_path, 0o600)
            logger.info("Saved Claude OAuth tokens")
    
    def has_valid_tokens(self) -> bool:
        """Check if we have valid (or refreshable) tokens."""
        return self._tokens is not None
    
    async def get_access_token(self) -> str:
        """Get valid access token, refreshing if needed."""
        if not self._tokens:
            raise ValueError("No OAuth tokens - run authenticate() first")
        
        if self._tokens.is_expired():
            await self._refresh_tokens()
        
        return self._tokens.access_token
    
    async def _refresh_tokens(self):
        """Refresh expired access token using refresh token."""
        if not self._tokens:
            raise ValueError("No tokens to refresh")
        
        logger.info("Refreshing Claude OAuth access token...")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.refresh_token,
                    "client_id": CLIENT_ID,
                },
                timeout=30.0,
            )
            
            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.text}")
                raise ValueError(f"Token refresh failed: {response.status_code}")
            
            data = response.json()
            self._tokens = OAuthTokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", self._tokens.refresh_token),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 28800)),
            )
            self._save_tokens()
            logger.info("Token refresh successful")
    
    def start_auth_flow(self) -> str:
        """Start OAuth flow and return the authorization URL.
        
        Call this first, then either:
        - complete_auth_flow() with the redirect URL (for Telegram flow)
        - Or use authenticate() for interactive console flow
        
        Returns:
            Authorization URL for user to visit
        """
        # Generate PKCE pair
        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(16)
        
        # Store for later verification
        self._pending_auth = {
            "verifier": verifier,
            "state": state,
        }
        
        # Build authorization URL
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "user:inference user:profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"
    
    async def complete_auth_flow(self, redirect_url: str) -> str:
        """Complete OAuth flow with the redirect URL from browser.
        
        Args:
            redirect_url: The full URL from browser after authentication
                         (e.g., http://localhost:19532/callback?code=...&state=...)
        
        Returns:
            Access token
        """
        if not self._pending_auth:
            raise ValueError("No pending auth flow - call start_auth_flow() first")
        
        # Parse the redirect URL
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        
        auth_code = params.get("code", [None])[0]
        received_state = params.get("state", [None])[0]
        
        if not auth_code:
            # Check for error
            error = params.get("error", [None])[0]
            error_desc = params.get("error_description", ["Unknown error"])[0]
            raise ValueError(f"OAuth error: {error} - {error_desc}")
        
        # Verify state
        if received_state != self._pending_auth["state"]:
            raise ValueError("OAuth state mismatch - possible CSRF attack")
        
        # Exchange code for tokens
        verifier = self._pending_auth["verifier"]
        self._pending_auth = None  # Clear pending auth
        
        logger.info("Exchanging authorization code for tokens...")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": auth_code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
                timeout=30.0,
            )
            
            if response.status_code != 200:
                logger.error(f"Token exchange failed: {response.text}")
                raise ValueError(f"Token exchange failed: {response.status_code}")
            
            data = response.json()
            self._tokens = OAuthTokens(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 28800)),
            )
            self._save_tokens()
        
        logger.info("Claude Max authentication successful!")
        return self._tokens.access_token
    
    async def authenticate_via_telegram(self) -> str:
        """Run OAuth flow via Telegram messages.
        
        Requires send_message and receive_message callbacks to be set.
        
        Returns:
            Access token
        """
        if not self._send_message or not self._receive_message:
            raise ValueError("Telegram callbacks not configured")
        
        # Start auth flow
        auth_url = self.start_auth_flow()
        
        # Send instructions via Telegram
        message = (
            "🔐 *Claude Max Authentication Required*\n\n"
            "1. Click this link to authenticate:\n"
            f"{auth_url}\n\n"
            "2. After logging in, you'll be redirected to a page that may not load.\n"
            "3. Copy the *entire URL* from your browser's address bar.\n"
            "4. Paste it here.\n\n"
            "_The URL will look like: http://localhost:19532/callback?code=...&state=..._"
        )
        await self._send_message(message)
        
        # Wait for user to paste the redirect URL
        redirect_url = await self._receive_message()
        
        # Complete the flow
        try:
            token = await self.complete_auth_flow(redirect_url.strip())
            await self._send_message("✅ Authentication successful! Claude Max is now connected.")
            return token
        except Exception as e:
            await self._send_message(f"❌ Authentication failed: {e}")
            raise
    
    async def authenticate(self) -> str:
        """Run OAuth flow - uses Telegram if configured, otherwise console.
        
        Returns:
            Access token
        """
        # If Telegram callbacks are set, use Telegram flow
        if self._send_message and self._receive_message:
            return await self.authenticate_via_telegram()
        
        # Otherwise, use console flow with instructions
        auth_url = self.start_auth_flow()
        
        print("\n" + "=" * 60)
        print("CLAUDE MAX AUTHENTICATION")
        print("=" * 60)
        print(f"\n1. Visit this URL to authenticate:\n")
        print(f"   {auth_url}\n")
        print("2. After logging in, copy the ENTIRE URL from your browser")
        print("   (it will look like http://localhost:19532/callback?code=...)")
        print("\n3. Paste it here:")
        print("=" * 60)
        
        redirect_url = input("\nRedirect URL: ").strip()
        
        return await self.complete_auth_flow(redirect_url)


async def ensure_claude_max_auth(
    token_path: Optional[Path] = None,
    send_message: Optional[Callable[[str], Awaitable[None]]] = None,
    receive_message: Optional[Callable[[], Awaitable[str]]] = None,
) -> ClaudeOAuth:
    """Ensure we have valid Claude Max authentication.
    
    If tokens exist and are valid/refreshable, returns immediately.
    Otherwise, runs OAuth flow (via Telegram if callbacks provided).
    
    Args:
        token_path: Path to store tokens
        send_message: Async function to send messages (for Telegram flow)
        receive_message: Async function to receive user input (for Telegram flow)
    """
    oauth = ClaudeOAuth(
        token_path=token_path,
        send_message=send_message,
        receive_message=receive_message,
    )
    
    if oauth.has_valid_tokens():
        # Try to get token (will refresh if needed)
        try:
            await oauth.get_access_token()
            return oauth
        except Exception as e:
            logger.warning(f"Existing tokens invalid: {e}")
    
    # Need fresh authentication
    await oauth.authenticate()
    return oauth
