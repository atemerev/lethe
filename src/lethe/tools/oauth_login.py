"""OAuth login dispatcher for Lethe."""

from lethe.tools.oauth_login_anthropic import run_anthropic_oauth_login
from lethe.tools.oauth_login_openai import run_openai_oauth_login


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

