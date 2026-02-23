"""Provider OAuth login flows for Lethe.

Supported providers:
- Anthropic Claude Max/Pro (PKCE code flow)
- OpenAI Codex (ChatGPT device flow)
"""

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
from lethe.memory.openai_oauth import (
    CLIENT_ID as OPENAI_CLIENT_ID,
    DEVICE_TOKEN_URL,
    DEVICE_USERCODE_URL,
    ISSUER as OPENAI_ISSUER,
    TOKEN_FILE as OPENAI_TOKEN_FILE,
    TOKEN_URL as OPENAI_TOKEN_URL,
    extract_account_id_from_tokens,
)

# Anthropic OAuth endpoints
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
ANTHROPIC_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
ANTHROPIC_SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"

# OpenAI device flow constants
OPENAI_DEVICE_REDIRECT_URI = f"{OPENAI_ISSUER}/deviceauth/callback"
OPENAI_POLL_SAFETY_MARGIN_SECONDS = 3
OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS = 900


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


async def _openai_start_device_flow() -> dict:
    """Start OpenAI device auth and return code metadata."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            DEVICE_USERCODE_URL,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "lethe-oauth-login",
            },
            json={"client_id": OPENAI_CLIENT_ID},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Device auth start failed: {response.status_code} {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected device auth response: {data}")
    return data


async def _openai_poll_for_authorization_code(
    device_auth_id: str,
    user_code: str,
    interval_seconds: int,
    timeout_seconds: float = OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS,
) -> dict:
    """Poll OpenAI device endpoint until authorization code is available."""
    wait_seconds = max(interval_seconds, 1) + OPENAI_POLL_SAFETY_MARGIN_SECONDS
    timeout_seconds = max(float(timeout_seconds), 1.0)
    deadline = time.monotonic() + timeout_seconds

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            response = await client.post(
                DEVICE_TOKEN_URL,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "lethe-oauth-login",
                },
                json={
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                },
            )

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"Unexpected device token response: {data}")
                return data

            # Pending auth in current OpenAI device endpoint behavior.
            if response.status_code in (403, 404):
                now = time.monotonic()
                if now >= deadline:
                    break
                sleep_seconds = min(wait_seconds, max(deadline - now, 0.0))
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                continue

            raise RuntimeError(
                f"Device authorization polling failed: {response.status_code} {response.text}"
            )
    raise RuntimeError(
        f"Timed out waiting for OpenAI device authorization after {int(timeout_seconds)}s"
    )


async def _openai_exchange_authorization_code(authorization_code: str, code_verifier: str) -> dict:
    """Exchange OpenAI authorization code for access/refresh tokens."""
    body = {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": OPENAI_DEVICE_REDIRECT_URI,
        "client_id": OPENAI_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            OPENAI_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {response.status_code} {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected token response: {data}")
    return data


def run_openai_oauth_login():
    """Run OpenAI Codex OAuth device flow."""
    print("\nOpenAI OAuth Login (ChatGPT Plus/Pro Codex)\n")
    print("This uses device flow, suitable for local and headless environments.")
    print("1) Open the verification URL")
    print("2) Enter the code shown below")
    print("3) Return to this terminal and wait for completion\n")

    try:
        device = asyncio.run(_openai_start_device_flow())
    except Exception as e:
        print(f"Failed to start device authorization: {e}")
        sys.exit(1)

    device_auth_id = str(device.get("device_auth_id", ""))
    user_code = str(device.get("user_code", ""))
    interval = int(device.get("interval", "5") or 5)
    verify_url = (
        device.get("verification_uri")
        or device.get("verification_uri_complete")
        or f"{OPENAI_ISSUER}/codex/device"
    )

    if not device_auth_id or not user_code:
        print(f"Invalid device authorization response: {device}")
        sys.exit(1)

    print(f"Verification URL: {verify_url}")
    print(f"User code: {user_code}\n")
    try:
        webbrowser.open(str(verify_url))
    except Exception:
        pass

    print("Waiting for authorization (Ctrl+C to cancel)...")
    try:
        auth_data = asyncio.run(
            _openai_poll_for_authorization_code(device_auth_id, user_code, interval)
        )
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\nAuthorization failed: {e}")
        sys.exit(1)

    authorization_code = str(auth_data.get("authorization_code", ""))
    code_verifier = str(auth_data.get("code_verifier", ""))
    if not authorization_code or not code_verifier:
        print(f"Invalid authorization completion payload: {auth_data}")
        sys.exit(1)

    print("Exchanging authorization code for tokens...")
    try:
        token_data = asyncio.run(
            _openai_exchange_authorization_code(authorization_code, code_verifier)
        )
    except Exception as e:
        print(f"Token exchange failed: {e}")
        sys.exit(1)

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in", 3600))
    if not access_token:
        print(f"No access token in response: {token_data}")
        sys.exit(1)

    account_id = extract_account_id_from_tokens(token_data)
    if not account_id:
        account_id = extract_account_id_from_tokens({"access_token": access_token})

    stored = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
        "account_id": account_id,
    }
    OPENAI_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPENAI_TOKEN_FILE.write_text(json.dumps(stored, indent=2))
    OPENAI_TOKEN_FILE.chmod(0o600)

    print(f"\nOAuth tokens saved to {OPENAI_TOKEN_FILE}")
    print(f"Access token: {access_token[:20]}...")
    print(f"Refresh token: {'yes' if refresh_token else 'no'}")
    print(f"Expires in: {expires_in}s")
    print(f"Account ID: {account_id or '(not found)'}")
    print("\nSet LLM_PROVIDER=openai in your .env (no OPENAI_API_KEY needed).")


def run_oauth_login(provider: str = "anthropic"):
    """Run provider OAuth login flow."""
    provider = (provider or "anthropic").strip().lower()
    if provider == "anthropic":
        run_anthropic_oauth_login()
        return
    if provider == "openai":
        run_openai_oauth_login()
        return
    raise ValueError(f"Unsupported OAuth provider: {provider}")
