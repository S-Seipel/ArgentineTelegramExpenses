from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_id: int = Field(..., alias="TELEGRAM_ALLOWED_USER_ID")

    database_url: str = Field(
        default="postgresql+psycopg://expenses:expenses@postgres:5432/expenses",
        alias="DATABASE_URL",
    )

    ollama_base_url: str = Field(
        default="http://host.docker.internal:11434", alias="OLLAMA_BASE_URL"
    )
    ollama_model: str = Field(default="qwen3:4b", alias="OLLAMA_MODEL")
    ollama_timeout: float = Field(default=180.0, alias="OLLAMA_TIMEOUT")
    ollama_max_retries: int = Field(default=2, alias="OLLAMA_MAX_RETRIES")
    whisper_model_size: str = Field(default="base", alias="WHISPER_MODEL_SIZE")
    whisper_device: str = Field(default="cpu", alias="WHISPER_DEVICE")
    whisper_compute_type: str = Field(
        default="int8", alias="WHISPER_COMPUTE_TYPE"
    )

    timezone: str = Field(
        default="America/Argentina/Buenos_Aires", alias="TIMEZONE"
    )

    max_message_length: int = Field(default=2000, alias="MAX_MESSAGE_LENGTH")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    def __repr__(self) -> str:
        return (
            f"Settings(env={self.app_env}, model={self.ollama_model}, "
            f"tz={self.timezone})"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
