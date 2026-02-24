"""Anthropic OAuth login flow for Lethe."""

import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
import webbrowser
from urllib.parse import urlencode

import httpx

from lethe.memory.anthropic_oauth import (
    CLIENT_ID as ANTHROPIC_CLIENT_ID,
    TOKEN_FILE as ANTHROPIC_TOKEN_FILE,
)

# Anthropic OAuth endpoints
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
ANTHROPIC_SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_anthropic_authorize_url(verifier: str, challenge: str) -> str:
    """Build Anthropic OAuth authorization URL."""
    params = {
        "code": "true",
        "client_id": ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "scope": ANTHROPIC_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    return f"{ANTHROPIC_AUTHORIZE_URL}?{urlencode(params)}"


async def _exchange_anthropic_code(code: str, verifier: str) -> dict:
    """Exchange Anthropic authorization code for tokens."""
    if "#" in code:
        auth_code, state = code.split("#", 1)
    else:
        auth_code = code
        state = None

    body = {
        "code": auth_code,
        "grant_type": "authorization_code",
        "client_id": ANTHROPIC_CLIENT_ID,
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "code_verifier": verifier,
    }
    if state:
        body["state"] = state

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(ANTHROPIC_TOKEN_URL, json=body)

    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {response.status_code} {response.text}")

    return response.json()


def run_anthropic_oauth_login():
    """Run interactive Anthropic OAuth flow."""
    print("\nAnthropic OAuth Login (Claude Max/Pro)\n")
    print("This will open your browser to sign in with your Anthropic account.")
    print("After signing in, paste the authorization code below.\n")

    verifier, challenge = _generate_pkce()
    url = _build_anthropic_authorize_url(verifier, challenge)

    print(f"Open this URL:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        print("(Could not open browser automatically - open the URL manually)")

    code = input("Authorization code: ").strip()
    if not code:
        print("No code provided. Aborting.")
        sys.exit(1)

    print("\nExchanging code for tokens...")
    try:
        result = asyncio.run(_exchange_anthropic_code(code, verifier))
    except Exception as e:
        print(f"Token exchange failed: {e}")
        sys.exit(1)

    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token")
    expires_in = int(result.get("expires_in", 3600))
    if not access_token:
        print(f"No access token in response: {result}")
        sys.exit(1)

    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
    }
    ANTHROPIC_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANTHROPIC_TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    ANTHROPIC_TOKEN_FILE.chmod(0o600)

    print(f"\nOAuth tokens saved to {ANTHROPIC_TOKEN_FILE}")
    print(f"Access token: {access_token[:20]}...")
    print(f"Refresh token: {'yes' if refresh_token else 'no'}")
    print(f"Expires in: {expires_in}s")
    print("\nSet LLM_PROVIDER=anthropic in your .env (no ANTHROPIC_API_KEY needed).")

