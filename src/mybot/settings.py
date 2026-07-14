"""Environment-backed settings with secret-safe diagnostics."""

from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://mybot:mybot@127.0.0.1:5432/mybot"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    otel_exporter_otlp_endpoint: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    qq_access_token: SecretStr | None = None

    def safe_for_logging(self) -> dict[str, Any]:
        """Return settings suitable for structured logs without secret values."""

        values: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            values[name] = "**********" if isinstance(value, SecretStr) else value
        return values
