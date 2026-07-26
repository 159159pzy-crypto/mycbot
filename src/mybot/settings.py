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
    ingest_stream: str = "mybot:ingest"
    outbound_stream: str = "mybot:outbound"
    ingest_group: str = "agent-workers"
    outbound_group: str = "gateway"
    stream_maxlen: int = Field(default=10_000, ge=100, le=1_000_000)
    stream_delivery_max_attempts: int = Field(default=5, ge=1, le=100)
    stream_dedupe_ttl_seconds: int = Field(default=3_600, ge=60, le=604_800)
    stream_block_ms: int = Field(default=1_000, ge=10, le=60_000)
    stream_claim_min_idle_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    otel_exporter_otlp_endpoint: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    qq_access_token: SecretStr | None = None
    napcat_ws_url: SecretStr | None = Field(default=None, validation_alias="NAPCAT_WS_URL")
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str = "deepseek-chat"
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=1_024, ge=1, le=32_768)
    llm_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    agent_system_prompt: str = (
        "You are MyBot, a helpful, concise assistant chatting on QQ and Telegram. "
        "Answer in the language the user writes in."
    )
    agent_history_max_messages: int = Field(default=40, ge=1, le=500)
    agent_history_token_budget: int = Field(default=6_000, ge=256, le=200_000)
    agent_daily_token_ceiling: int = Field(default=200_000, ge=0, le=100_000_000)
    agent_conversation_daily_token_ceiling: int = Field(
        default=20_000, ge=0, le=100_000_000
    )
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "text-embedding-3-small"
    memory_enabled: bool = True
    memory_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    memory_retrieval_limit: int = Field(default=5, ge=1, le=20)
    memory_token_budget: int = Field(default=1_200, ge=100, le=20_000)
    memory_decay_days: int = Field(default=90, ge=1, le=3_650)
    memory_decay_factor: float = Field(default=0.8, gt=0.0, le=1.0)
    memory_confidence_floor: float = Field(default=0.2, ge=0.0, le=1.0)
    memory_revoked_retention_days: int = Field(default=30, ge=1, le=3_650)
    memory_maintenance_interval_seconds: int = Field(default=3_600, ge=60, le=86_400)
    rate_limit_user_per_minute: int = Field(default=20, ge=0, le=10_000)
    rate_limit_chat_per_minute: int = Field(default=30, ge=0, le=10_000)
    group_cooldown_seconds: float = Field(default=3.0, ge=0.0, le=3_600.0)
    input_max_chars: int = Field(default=4_000, ge=100, le=100_000)
    loop_guard_enabled: bool = True
    proactive_enabled: bool = False
    proactive_min_interval_hours: float = Field(default=24.0, ge=1.0, le=720.0)
    proactive_quiet_start_hour: int = Field(default=22, ge=0, le=23)
    proactive_quiet_end_hour: int = Field(default=8, ge=0, le=23)
    proactive_message: str = (
        "It's been quiet here for a while — anything I can help with?"
    )
    operator_token: SecretStr | None = None
    operator_auth_max_failures: int = Field(default=10, ge=1, le=1_000)
    operator_auth_window_seconds: float = Field(default=60.0, ge=1.0, le=3_600.0)
    tool_approvals_cache_ttl_seconds: float = Field(default=10.0, ge=1.0, le=600.0)
    plugin_broker_url: str | None = None
    plugin_config: str | None = None
    plugin_capability_grants: str = "{}"
    plugin_invoke_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    plugin_poll_wait_seconds: float = Field(default=20.0, gt=0.0, le=60.0)
    plugin_catalog_ttl_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    plugin_result_max_chars: int = Field(default=16_000, ge=1_000, le=200_000)
    searxng_url: str = "http://127.0.0.1:8080"
    agent_granted_capabilities: str = "web.search,web.fetch"
    tool_max_calls_per_turn: int = Field(default=5, ge=1, le=20)
    tool_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    turn_deadline_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    tool_fetch_max_bytes: int = Field(default=2_000_000, ge=10_000, le=20_000_000)
    search_max_results: int = Field(default=5, ge=1, le=10)

    def granted_capabilities(self) -> tuple[str, ...]:
        return tuple(
            capability.strip()
            for capability in self.agent_granted_capabilities.split(",")
            if capability.strip()
        )

    def plugin_grants(self) -> dict[str, tuple[str, ...]]:
        """Operator-granted capabilities per plugin id, parsed from JSON."""

        import json
        from typing import cast

        try:
            decoded = json.loads(self.plugin_capability_grants)
        except ValueError:
            return {}
        if not isinstance(decoded, dict):
            return {}
        grants: dict[str, tuple[str, ...]] = {}
        for plugin_id, capabilities in cast(dict[object, object], decoded).items():
            if isinstance(capabilities, list):
                grants[str(plugin_id)] = tuple(
                    str(capability) for capability in cast(list[object], capabilities)
                )
        return grants
    qq_connection_id: str = "qq-main"
    telegram_connection_id: str = "telegram-main"
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_poll_timeout_seconds: float = Field(default=50.0, gt=0.0, le=90.0)
    gateway_reconnect_initial_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    gateway_reconnect_max_seconds: float = Field(default=30.0, gt=0.0, le=600.0)

    @field_validator(
        "otel_exporter_otlp_endpoint",
        "telegram_bot_token",
        "qq_access_token",
        "napcat_ws_url",
        "llm_api_key",
        "llm_base_url",
        "embedding_api_key",
        "embedding_base_url",
        "plugin_broker_url",
        "plugin_config",
        "operator_token",
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
