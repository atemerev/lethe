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

    # Letta
    letta_base_url: str = Field(
        default="http://localhost:8283",
        description="Letta server URL",
    )
    letta_api_key: Optional[str] = Field(
        default=None,
        description="Letta API key (for cloud or auth-enabled server)",
    )

    # Agent
    lethe_agent_name: str = Field(default="lethe", description="Agent name")
    lethe_agent_model: str = Field(default="letta/letta-free", description="Model handle (e.g., letta/letta-free, anthropic/claude-sonnet-4-20250514)")
    lethe_config_dir: Path = Field(default=Path("./config"), description="Config directory")

    # Database
    db_path: Path = Field(default=Path("./data/lethe.db"), description="SQLite database path")

    # Browser
    browser_headless: bool = Field(default=True, description="Run browser in headless mode")

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
