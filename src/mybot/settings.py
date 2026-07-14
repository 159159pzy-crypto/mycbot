"""Environment-backed settings with secret-safe diagnostics."""

from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
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
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    database_connect_timeout_seconds: float = Field(default=3.0, gt=0.0, le=30.0)
    database_read_timeout_seconds: float = Field(default=3.0, gt=0.0, le=30.0)
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    redis_read_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://mybot:mybot@127.0.0.1:5432/mybot"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    otel_exporter_otlp_endpoint: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    qq_access_token: SecretStr | None = None

    @field_validator(
        "otel_exporter_otlp_endpoint",
        "telegram_bot_token",
        "qq_access_token",
        mode="before",
    )
    @classmethod
    def blank_optional_secret_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def safe_for_logging(self) -> dict[str, Any]:
        """Return settings suitable for structured logs without secret values."""

        values: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            values[name] = "**********" if isinstance(value, SecretStr) else value
        return values
