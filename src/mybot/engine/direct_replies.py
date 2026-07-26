"""Deterministic reply construction for the pre-agent messaging spine."""

from mybot.contracts import (
    MessageEnvelope,
    PlatformCapabilities,
    ReplyPlan,
    TextSegment,
    TypingProfile,
)
from mybot.infrastructure.health import ReadinessResponse

_EXCERPT_LIMIT = 120
_KNOWN_COMMANDS = ("/ping", "/status", "/forget")


def build_direct_reply(
    envelope: MessageEnvelope,
    *,
    command: str | None,
    readiness: ReadinessResponse | None,
    capabilities: PlatformCapabilities,
) -> ReplyPlan:
    """Build the deterministic milestone-2 reply for an answerable message."""

    if command == "ping":
        text = "pong"
    elif command == "status":
        text = _status_text(readiness)
    elif command is not None:
        text = f"unknown command /{command}; supported commands: {', '.join(_KNOWN_COMMANDS)}"
    else:
        text = _echo_text(envelope)
    return ReplyPlan(
        text_segments=(text,),
        typing=TypingProfile(enabled=capabilities.typing),
    )


def forget_reply(
    revoked_count: int | None, *, capabilities: PlatformCapabilities
) -> ReplyPlan:
    """Deterministic /forget acknowledgment; None means memory is not wired."""

    if revoked_count is None:
        text = "memory is disabled on this deployment, so there is nothing to forget"
    elif revoked_count == 0:
        text = "done — I had no stored memories about you"
    else:
        text = f"done — revoked {revoked_count} stored memories about you"
    return ReplyPlan(
        text_segments=(text,),
        typing=TypingProfile(enabled=capabilities.typing),
    )


def _status_text(readiness: ReadinessResponse | None) -> str:
    if readiness is None:
        return "status: readiness report unavailable"
    dependencies = readiness.dependencies
    return (
        f"status: {readiness.status} | "
        f"database: {dependencies.database.status.value} | "
        f"redis: {dependencies.redis.status.value}"
    )


def _echo_text(envelope: MessageEnvelope) -> str:
    excerpt = ""
    for segment in envelope.segments:
        if isinstance(segment, TextSegment):
            excerpt = segment.text
            break
    if len(excerpt) > _EXCERPT_LIMIT:
        excerpt = excerpt[: _EXCERPT_LIMIT - 1] + "…"
    if excerpt:
        return f'echo: "{excerpt}" (rule-based reply; the full agent arrives in a later milestone)'
    return "received (rule-based reply; the full agent arrives in a later milestone)"
