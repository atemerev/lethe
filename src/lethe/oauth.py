"""OAuth authentication for Claude Max subscription.

Handles PKCE OAuth flow for Claude Max/Pro subscriptions.
Stores tokens and handles automatic refresh.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

# Claude OAuth endpoints
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/api/oauth/token"

# Claude Code CLI client_id (public)
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Local callback server
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 19532  # Random port for OAuth callback
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

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
    """Handles Claude Max OAuth authentication."""
    
    def __init__(self, token_path: Optional[Path] = None):
        self.token_path = token_path or DEFAULT_TOKEN_PATH
        self._tokens: Optional[OAuthTokens] = None
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
    
    async def authenticate(self, open_browser: bool = True) -> str:
        """Run OAuth flow to get tokens.
        
        Args:
            open_browser: If True, opens browser automatically. If False, prints URL.
            
        Returns:
            Access token
        """
        # Generate PKCE pair
        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(16)
        
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
        auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        
        # Start local server to capture callback
        callback_received = asyncio.Event()
        auth_code = None
        received_state = None
        
        async def handle_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            nonlocal auth_code, received_state
            
            # Read HTTP request
            request_line = await reader.readline()
            request = request_line.decode()
            
            # Parse URL
            if "GET /callback" in request:
                # Extract query params
                path = request.split()[1]
                parsed = urlparse(path)
                params = parse_qs(parsed.query)
                
                auth_code = params.get("code", [None])[0]
                received_state = params.get("state", [None])[0]
                
                # Send success response
                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html\r\n"
                    "\r\n"
                    "<html><body><h1>Authentication successful!</h1>"
                    "<p>You can close this window and return to Lethe.</p>"
                    "<script>window.close();</script></body></html>"
                )
            else:
                response = "HTTP/1.1 404 Not Found\r\n\r\n"
            
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            
            if auth_code:
                callback_received.set()
        
        # Start callback server
        server = await asyncio.start_server(handle_callback, CALLBACK_HOST, CALLBACK_PORT)
        
        try:
            # Show auth URL
            print("\n" + "=" * 60)
            print("CLAUDE MAX AUTHENTICATION")
            print("=" * 60)
            print(f"\nPlease visit this URL to authenticate:\n")
            print(f"  {auth_url}\n")
            
            if open_browser:
                print("Opening browser...")
                webbrowser.open(auth_url)
            
            print("Waiting for authentication...")
            print("=" * 60 + "\n")
            
            # Wait for callback (timeout 5 minutes)
            try:
                await asyncio.wait_for(callback_received.wait(), timeout=300)
            except asyncio.TimeoutError:
                raise ValueError("Authentication timed out")
            
        finally:
            server.close()
            await server.wait_closed()
        
        # Verify state
        if received_state != state:
            raise ValueError("OAuth state mismatch - possible CSRF attack")
        
        if not auth_code:
            raise ValueError("No authorization code received")
        
        # Exchange code for tokens
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


async def ensure_claude_max_auth(token_path: Optional[Path] = None) -> ClaudeOAuth:
    """Ensure we have valid Claude Max authentication.
    
    If tokens exist and are valid/refreshable, returns immediately.
    Otherwise, runs OAuth flow.
    """
    oauth = ClaudeOAuth(token_path)
    
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
