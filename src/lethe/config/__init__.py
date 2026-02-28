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
        env_file_priority="env_file",  # .env takes precedence over shell environment
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

    # LLM
    openrouter_api_key: Optional[str] = Field(
        default=None,
        description="OpenRouter API key (reads from OPENROUTER_API_KEY env var)",
    )
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="Gemini API key (reads from GEMINI_API_KEY env var)",
    )
    llm_model: str = Field(
        default="",
        description="Main LLM model (empty = use provider default)",
    )
    llm_model_aux: str = Field(
        default="",
        description="Auxiliary LLM model for heartbeats, summarization (empty = use main model)",
    )
    llm_model_dmn: str = Field(
        default="",
        description="DMN LLM model override (empty = use aux model)",
    )
    llm_api_base: str = Field(
        default="",
        description="Custom API base URL for local/compatible providers (empty = use provider default)",
    )
    llm_context_limit: int = Field(
        default=100000,
        description="Context window size in tokens",
    )
    llm_messages_load: int = Field(
        default=20,
        description="Number of recent messages to load verbatim at startup",
    )
    llm_messages_summarize: int = Field(
        default=100,
        description="Number of messages before recent to summarize at startup",
    )

    # Agent
    lethe_agent_name: str = Field(default="lethe", description="Agent name")
    lethe_config_dir: Path = Field(default=Path("./config"), description="Config directory")
    workspace_dir: Path = Field(default=Path("./workspace"), description="Agent workspace directory")

    # Memory
    memory_dir: Path = Field(default=Path("./data/memory"), description="Memory storage directory")

    # Conversation
    debounce_seconds: float = Field(default=5.0, description="Wait time for additional messages")

    # Database
    db_path: Path = Field(default=Path("./data/lethe.db"), description="SQLite database path")

    # Background cognition modules
    amygdala_enabled: bool = Field(
        default=True,
        description="Enable Amygdala emotional salience actor (heartbeat-triggered, aux model)",
    )

    # Proactive messaging limits (hard enforcement, not prompt-dependent)
    proactive_max_per_day: int = Field(
        default=4,
        description="Maximum proactive messages to user per calendar day (0 = disabled)",
    )
    proactive_cooldown_minutes: int = Field(
        default=60,
        description="Minimum minutes between proactive messages",
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
