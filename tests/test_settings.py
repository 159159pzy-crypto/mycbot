import json

import pytest

from mybot.settings import Settings


def test_settings_redact_secrets_from_repr_and_log_serialization(monkeypatch) -> None:
    database_secret = "postgresql+psycopg://bot:very-secret@db:5432/mybot"
    redis_secret = "redis://:redis-secret@redis:6379/0"
    telegram_secret = "telegram-super-secret"
    monkeypatch.setenv("MYBOT_DATABASE_URL", database_secret)
    monkeypatch.setenv("MYBOT_REDIS_URL", redis_secret)
    monkeypatch.setenv("MYBOT_TELEGRAM_BOT_TOKEN", telegram_secret)

    settings = Settings()
    rendered = repr(settings)
    serialized = json.dumps(settings.safe_for_logging(), sort_keys=True)
    json_dump = json.dumps(settings.model_dump(mode="json"), sort_keys=True)

    for secret in (database_secret, redis_secret, telegram_secret):
        assert secret not in rendered
        assert secret not in serialized
        assert secret not in json_dump
    assert settings.database_url.get_secret_value() == database_secret


@pytest.mark.parametrize(
    "field_name",
    [
        "otel_exporter_otlp_endpoint",
        "telegram_bot_token",
        "qq_access_token",
    ],
)
def test_blank_optional_secret_environment_values_become_none(
    monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    monkeypatch.setenv(f"MYBOT_{field_name.upper()}", "   ")

    settings = Settings()

    assert getattr(settings, field_name) is None


def test_stream_settings_defaults_are_bounded() -> None:
    settings = Settings()

    assert settings.ingest_stream == "mybot:ingest"
    assert settings.outbound_stream == "mybot:outbound"
    assert settings.ingest_group == "agent-workers"
    assert settings.outbound_group == "gateway"
    assert settings.stream_maxlen == 10_000
    assert settings.stream_delivery_max_attempts == 5
    assert settings.stream_dedupe_ttl_seconds == 3_600
    assert settings.stream_block_ms == 1_000
    assert settings.stream_claim_min_idle_ms == 30_000


def test_stream_bounds_reject_out_of_range_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYBOT_STREAM_DELIVERY_MAX_ATTEMPTS", "0")

    with pytest.raises(ValueError):
        Settings()


def test_llm_settings_default_to_a_disabled_agent_with_bounded_knobs() -> None:
    settings = Settings()

    assert settings.llm_base_url is None
    assert settings.llm_api_key is None
    assert settings.llm_model == "deepseek-chat"
    assert 0.0 <= settings.llm_temperature <= 2.0
    assert 1 <= settings.model_channel_cooldown_seconds <= 3_600
    assert 1.0 <= settings.model_channels_cache_ttl_seconds <= 600.0
    assert settings.model_api_keys is None
    assert settings.agent_history_token_budget >= 256
    assert settings.agent_daily_token_ceiling >= 0
    assert "MyBot" in settings.agent_system_prompt
    assert settings.vision_mode == "describe"
    assert settings.vision_max_description_chars >= 100
    assert settings.vision_max_image_bytes == 10_000_000
    assert settings.vision_image_download_timeout_seconds == 30.0
    assert settings.fallback_llm_failure == "语言模型暂时不可用, 请稍后再试。"


def test_model_secret_map_resolves_only_named_environment_references() -> None:
    settings = Settings(model_api_keys='{"PRIMARY_KEY": "secret-value"}')

    assert settings.model_secret("PRIMARY_KEY") == "secret-value"
    assert settings.model_secret("MISSING_KEY") is None


def test_blank_llm_base_url_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYBOT_LLM_BASE_URL", "   ")
    monkeypatch.setenv("MYBOT_LLM_API_KEY", "")

    settings = Settings()

    assert settings.llm_base_url is None
    assert settings.llm_api_key is None


def test_tool_settings_have_bounded_defaults_and_parsed_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    assert settings.searxng_url == "http://127.0.0.1:8080"
    assert settings.granted_capabilities() == (
        "web.search",
        "web.fetch",
        "memory.write",
        "knowledge.read",
    )
    assert 1 <= settings.tool_max_calls_per_turn <= 20
    assert settings.tool_timeout_seconds <= settings.turn_deadline_seconds

    monkeypatch.setenv("MYBOT_AGENT_GRANTED_CAPABILITIES", " web.search , ,custom.cap ")
    assert Settings().granted_capabilities() == ("web.search", "custom.cap")

    monkeypatch.setenv("MYBOT_TOOL_MAX_CALLS_PER_TURN", "0")
    with pytest.raises(ValueError):
        Settings()


def test_memory_settings_default_bounded_and_blank_embedding_endpoint_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    assert settings.embedding_base_url is None
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.memory_enabled is True
    assert 0.0 <= settings.memory_min_confidence <= 1.0
    assert settings.memory_confidence_floor < settings.memory_min_confidence
    assert settings.memory_maintenance_interval_seconds >= 60
    assert settings.memory_core_persona_token_budget >= 50
    assert settings.memory_core_user_profile_token_budget >= 50
    assert settings.memory_core_tools_approval_required is True
    assert settings.memory_flush_enabled is True
    assert settings.memory_flush_max_messages <= 200
    assert settings.memory_consolidation_enabled is True
    assert settings.memory_consolidation_interval_seconds >= 3_600
    assert settings.memory_consolidation_token_budget <= 32_000
    assert settings.personality_learning_enabled is False
    assert settings.personality_learning_message_limit >= 3
    assert settings.personality_learning_token_budget <= 32_000
    assert settings.proactive_context_messages >= 1

    monkeypatch.setenv("MYBOT_EMBEDDING_BASE_URL", "   ")
    monkeypatch.setenv("MYBOT_EMBEDDING_API_KEY", "")
    assert Settings().embedding_base_url is None
    assert Settings().embedding_api_key is None

    monkeypatch.setenv("MYBOT_MEMORY_DECAY_FACTOR", "0")
    with pytest.raises(ValueError):
        Settings()


def test_plugin_settings_parse_grants_and_blank_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    assert settings.plugin_broker_url is None
    assert settings.plugin_config is None
    assert settings.plugin_grants() == {}
    assert settings.plugin_invoke_timeout_seconds <= 120.0

    monkeypatch.setenv("MYBOT_PLUGIN_BROKER_URL", "   ")
    monkeypatch.setenv(
        "MYBOT_PLUGIN_CAPABILITY_GRANTS",
        '{"example.dice": [], "acme.tools": ["web.search", "memory.read"]}',
    )
    parsed = Settings()
    assert parsed.plugin_broker_url is None
    assert parsed.plugin_grants() == {
        "example.dice": (),
        "acme.tools": ("web.search", "memory.read"),
    }

    monkeypatch.setenv("MYBOT_PLUGIN_CAPABILITY_GRANTS", "not-json")
    assert Settings().plugin_grants() == {}


def test_guard_and_proactive_settings_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    assert settings.rate_limit_user_per_minute >= 0
    assert settings.group_cooldown_seconds >= 0
    assert settings.input_max_chars >= 100
    assert settings.loop_guard_enabled is True
    assert settings.proactive_enabled is False
    assert 0 <= settings.proactive_quiet_start_hour <= 23
    assert settings.proactive_min_interval_hours >= 1.0
    assert settings.proactive_message

    monkeypatch.setenv("MYBOT_PROACTIVE_QUIET_START_HOUR", "24")
    with pytest.raises(ValueError):
        Settings()


def test_operator_token_is_redacted_and_blank_becomes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    assert settings.operator_token is None
    assert settings.operator_auth_max_failures >= 1

    monkeypatch.setenv("MYBOT_OPERATOR_TOKEN", "   ")
    assert Settings().operator_token is None

    secret = "operator-super-secret"
    monkeypatch.setenv("MYBOT_OPERATOR_TOKEN", secret)
    configured = Settings()
    assert configured.operator_token is not None
    assert secret not in repr(configured)
    assert secret not in json.dumps(configured.safe_for_logging(), sort_keys=True)
