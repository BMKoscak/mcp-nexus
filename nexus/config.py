"""Central configuration, loaded from environment variables and `.env`.

Settings are read once at import time via `get_settings()`. Values the user
enters in the UI (API keys, toggles) live in SQLite and are layered on top at
runtime by the persistence layer; environment values always win.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="NEXUS_",
        extra="ignore",
        env_ignore_empty=True,
    )

    # Server
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    workspace_dir: Path = Path("./workspace")
    plugins_dir: Path = Path("./plugins")
    secret_key: SecretStr | None = None

    # LLM providers (provider-standard names, no NEXUS_ prefix)
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434", validation_alias=AliasChoices("OLLAMA_BASE_URL")
    )
    default_model: str = "ollama/llama3.2"

    # Outbound dispatcher
    auto_approve_outbound: bool = False
    approval_timeout_seconds: int = 120
    discord_webhook_url: SecretStr | None = None
    slack_webhook_url: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # Web fetcher
    fetch_timeout_seconds: float = 15.0
    fetch_max_bytes: int = 2_000_000
    fetch_block_private: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nexus.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.workspace_dir, self.plugins_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
