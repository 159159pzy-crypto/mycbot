from datetime import UTC, datetime

from mybot.adapters.qq.translate import QQ_CAPABILITIES
from mybot.adapters.telegram.translate import TELEGRAM_CAPABILITIES
from mybot.contracts import ChatKind, MessageEnvelope, Platform, TextSegment
from mybot.engine.direct_replies import build_direct_reply
from mybot.infrastructure.health import (
    DependencyHealth,
    DependencyStatus,
    ReadinessDependencies,
    ReadinessResponse,
)


def envelope(text: str) -> MessageEnvelope:
    return MessageEnvelope(
        id="telegram:telegram-main:777:55",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        chat_id="777",
        sender_identity_id="telegram:777",
        occurred_at=datetime(2026, 7, 26, 4, tzinfo=UTC),
        segments=(TextSegment(text=text),),
    )


def readiness(*, ready: bool) -> ReadinessResponse:
    status = DependencyStatus.UP if ready else DependencyStatus.DOWN
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        dependencies=ReadinessDependencies(
            database=DependencyHealth(status=DependencyStatus.UP),
            redis=DependencyHealth(status=status),
        ),
    )


def test_ping_command_returns_pong() -> None:
    plan = build_direct_reply(
        envelope("/ping"),
        command="ping",
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert plan.text_segments == ("pong",)


def test_status_command_renders_dependency_summary() -> None:
    plan = build_direct_reply(
        envelope("/status"),
        command="status",
        readiness=readiness(ready=False),
        capabilities=TELEGRAM_CAPABILITIES,
    )

    text = plan.text_segments[0]
    assert "not_ready" in text
    assert "database: up" in text
    assert "redis: down" in text


def test_status_without_report_degrades_gracefully() -> None:
    plan = build_direct_reply(
        envelope("/status"),
        command="status",
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert "unavailable" in plan.text_segments[0]


def test_unknown_command_states_supported_commands() -> None:
    plan = build_direct_reply(
        envelope("/dance"),
        command="dance",
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )

    text = plan.text_segments[0]
    assert "/ping" in text
    assert "/status" in text


def test_plain_message_echoes_a_truncated_excerpt_deterministically() -> None:
    long_text = "x" * 500
    plan = build_direct_reply(
        envelope(long_text),
        command=None,
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )
    again = build_direct_reply(
        envelope(long_text),
        command=None,
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert plan == again
    assert 1 <= len(plan.text_segments) <= 3
    text = plan.text_segments[0]
    assert "echo" in text
    assert len(text) < 240
    assert "later milestone" in text


def test_forget_reply_covers_disabled_zero_and_counted_cases() -> None:
    from mybot.engine.direct_replies import forget_reply

    disabled = forget_reply(None, capabilities=TELEGRAM_CAPABILITIES)
    empty = forget_reply(0, capabilities=TELEGRAM_CAPABILITIES)
    counted = forget_reply(5, capabilities=QQ_CAPABILITIES)

    assert "memory is disabled" in disabled.text_segments[0]
    assert "no stored memories" in empty.text_segments[0]
    assert "revoked 5 stored memories" in counted.text_segments[0]
    assert counted.typing.enabled is False


def test_unknown_command_lists_forget() -> None:
    plan = build_direct_reply(
        envelope("/dance"),
        command="dance",
        readiness=None,
        capabilities=TELEGRAM_CAPABILITIES,
    )

    assert "/forget" in plan.text_segments[0]


def test_typing_profile_follows_platform_capabilities() -> None:
    typing_platform = build_direct_reply(
        envelope("hi"), command=None, readiness=None, capabilities=TELEGRAM_CAPABILITIES
    )
    no_typing_platform = build_direct_reply(
        envelope("hi"), command=None, readiness=None, capabilities=QQ_CAPABILITIES
    )

    assert typing_platform.typing.enabled is True
    assert no_typing_platform.typing.enabled is False
