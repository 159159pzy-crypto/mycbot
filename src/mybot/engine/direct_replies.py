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
        text = f"未知命令 /{command}; 支持的命令: {', '.join(_KNOWN_COMMANDS)}"
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
        text = "当前部署未启用记忆, 没有需要遗忘的内容"
    elif revoked_count == 0:
        text = "完成: 没有找到关于你的已存记忆"
    else:
        text = f"完成: 已撤销 {revoked_count} 条关于你的记忆"
    return ReplyPlan(
        text_segments=(text,),
        typing=TypingProfile(enabled=capabilities.typing),
    )


def _status_text(readiness: ReadinessResponse | None) -> str:
    if readiness is None:
        return "状态: 暂时无法获取就绪报告"
    dependencies = readiness.dependencies
    return (
        f"状态: {readiness.status} | "
        f"数据库: {dependencies.database.status.value} | "
        f"Redis: {dependencies.redis.status.value}"
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
        return f'回显: "{excerpt}" (规则回复)'
    return "已收到 (规则回复)"
