"""Configuration management."""

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., description="Telegram bot token from BotFather")
    telegram_allowed_user_ids: str = Field(
        default="",
        description="Comma-separated list of allowed Telegram user IDs (empty = allow all)",
    )

    @property
    def allowed_user_ids(self) -> list[int]:
        """Parse allowed user IDs from comma-separated string."""
        if not self.telegram_allowed_user_ids.strip():
            return []
        return [int(x.strip()) for x in self.telegram_allowed_user_ids.split(",") if x.strip()]

    # Letta (default: Letta Cloud)
    letta_base_url: str = Field(
        default="https://api.letta.com",
        description="Letta server URL (default: Letta Cloud, use http://localhost:8283 for local server)",
    )
    letta_api_key: Optional[str] = Field(
        default=None,
        description="Letta API key (required for Letta Cloud, get from https://app.letta.com)",
    )

    # Agent
    lethe_agent_name: str = Field(default="lethe", description="Agent name")
    lethe_agent_model: str = Field(default="letta/letta-free", description="Model handle (e.g., letta/letta-free, anthropic/claude-sonnet-4-20250514)")
    lethe_config_dir: Path = Field(default=Path("./config"), description="Config directory")

    # Hippocampus (memory retrieval subagent)
    hippocampus_enabled: bool = Field(default=True, description="Enable hippocampus memory retrieval")
    hippocampus_agent_name: str = Field(default="lethe-hippocampus", description="Hippocampus agent name")
    hippocampus_model: str = Field(default="anthropic/claude-haiku-4-5-20251015", description="Cheap/fast model for hippocampus")

    # Database
    db_path: Path = Field(default=Path("./data/lethe.db"), description="SQLite database path")

    # Browser / Steel
    steel_base_url: str = Field(
        default="http://127.0.0.1:3000",
        description="Steel Browser API URL (local Docker or cloud). Use 127.0.0.1, not localhost (IPv6 issues).",
    )
    steel_api_key: Optional[str] = Field(
        default=None,
        description="Steel API key (required for Steel Cloud, optional for local)",
    )

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get application settings (cached singleton)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def load_config_file(name: str, settings: Optional[Settings] = None) -> str:
    """Load a configuration file from the config directory."""
    if settings is None:
        settings = get_settings()

    config_path = settings.lethe_config_dir / f"{name}.md"
    if config_path.exists():
        return config_path.read_text()
    return ""
