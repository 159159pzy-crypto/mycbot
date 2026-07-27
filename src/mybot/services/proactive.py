"""Policy-gated proactive check-ins scheduled by the maintenance worker."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from mybot.adapters import OutboundMessage
from mybot.contracts import (
    ChatKind,
    Platform,
    ReplyPlan,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.repositories.conversations import ConversationDetail
from mybot.repositories.turns import TurnOutcome

logger = structlog.get_logger("mybot.proactive")

OPTIN_KEY = "proactive.enabled_conversations"


class ProactiveConfig(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


class ConversationLookup(Protocol):
    async def by_stable_key(self, stable_key: str) -> ConversationDetail | None: ...


class OutboundStore(Protocol):
    async def record_outbound(
        self,
        conversation_id: UUID,
        plan: ReplyPlan,
        *,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID: ...


class TurnStore(Protocol):
    async def record_turn(
        self,
        conversation_id: UUID,
        decision: TurnDecision,
        *,
        outcome: TurnOutcome,
        inbound_message_id: UUID | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> UUID: ...


class Publisher(Protocol):
    async def publish(self, payload: str) -> str: ...


class OnceBackend(Protocol):
    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """UTC quiet window; start == end means no quiet window; wraps midnight."""

    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@dataclass(slots=True)
class ProactivePass:
    """One scheduled sweep over opted-in conversations."""

    config: ProactiveConfig
    conversations: ConversationLookup
    messages: OutboundStore
    turns: TurnStore
    outbound: Publisher
    once: OnceBackend
    enabled: bool = False
    message: str = "最近有点安静, 有什么想聊或需要我帮忙的吗?"
    min_interval_hours: float = 24.0
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    key_prefix: str = "mybot:proactive"
    now: Callable[[], datetime] = field(default=_utc_now)

    async def run_pass(self) -> int:
        """Send due check-ins; returns how many were sent."""

        if not self.enabled:
            return 0
        current = self.now()
        if in_quiet_hours(current.hour, self.quiet_start_hour, self.quiet_end_hour):
            return 0
        raw = await self.config.get(OPTIN_KEY)
        if not isinstance(raw, list):
            return 0
        sent = 0
        for entry in cast(list[object], raw):
            stable_key = str(entry).strip()
            if not stable_key:
                continue
            detail = await self.conversations.by_stable_key(stable_key)
            if detail is None:
                logger.warning("proactive_conversation_missing", stable_key=stable_key)
                continue
            if detail.ephemeral:
                logger.info("proactive_ephemeral_skipped", stable_key=stable_key)
                continue
            due = await self.once.acquire_once(
                f"{self.key_prefix}:{stable_key}",
                ttl_seconds=max(60, int(self.min_interval_hours * 3600)),
            )
            if not due:
                continue
            await self._send(detail)
            sent += 1
        if sent:
            logger.info("proactive_messages_sent", count=sent)
        return sent

    async def _send(self, detail: ConversationDetail) -> None:
        plan = ReplyPlan(text_segments=(self.message,))
        trace_id = str(uuid4())
        outbound_id = await self.messages.record_outbound(
            detail.id, plan, trace_id=trace_id
        )
        message = OutboundMessage(
            internal_message_id=outbound_id,
            platform=Platform(detail.platform),
            connection_id=detail.connection_id,
            chat_kind=ChatKind(detail.chat_kind),
            chat_id=detail.chat_id,
            reply_plan=plan,
            trace_id=trace_id,
        )
        await self.outbound.publish(message.model_dump_json())
        await self.turns.record_turn(
            detail.id,
            TurnDecision(
                action=TurnAction.DIRECT_REPLY,
                reason="scheduled proactive check-in",
                confidence=1.0,
                trigger=TurnTrigger.PROACTIVE,
            ),
            outcome="replied",
        )
