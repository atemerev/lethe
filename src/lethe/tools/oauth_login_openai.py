"""OpenAI OAuth login flow for Lethe."""

import asyncio
import json
import sys
import time
import webbrowser

import httpx

from lethe.memory.openai_oauth import (
    CLIENT_ID as OPENAI_CLIENT_ID,
    DEVICE_TOKEN_URL,
    DEVICE_USERCODE_URL,
    ISSUER as OPENAI_ISSUER,
    TOKEN_FILE as OPENAI_TOKEN_FILE,
    TOKEN_URL as OPENAI_TOKEN_URL,
    extract_account_id_from_tokens,
)

# OpenAI device flow constants
OPENAI_DEVICE_REDIRECT_URI = f"{OPENAI_ISSUER}/deviceauth/callback"
OPENAI_POLL_SAFETY_MARGIN_SECONDS = 3
OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS = 900


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

