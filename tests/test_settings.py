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
