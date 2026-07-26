"""Abuse and cost guards applied to answerable turns before any reply work."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

import structlog

from mybot.adapters import InboundEvent
from mybot.contracts import ChatKind, TurnAction, TurnDecision, TurnTrigger
from mybot.engine.prompt import envelope_text

logger = structlog.get_logger("mybot.guards")

RATE_LIMIT_REFUSAL = (
    "You're sending messages faster than I can reasonably answer — "
    "I'll stay quiet for a bit. Please slow down."
)
INPUT_TOO_LONG_REFUSAL = (
    "That message is too long for me to take in one turn. "
    "Please split it into smaller parts."
)
_ECHO_WINDOW = 5


class CounterBackend(Protocol):
    async def increment(self, key: str, amount: int, *, ttl_seconds: int) -> int: ...

    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


class RecentTexts(Protocol):
    async def recent_texts(
        self, conversation_id: UUID, *, limit: int = 40
    ) -> tuple["_TextRow", ...]: ...


class _TextRow(Protocol):
    @property
    def direction(self) -> str: ...

    @property
    def text(self) -> str: ...


@dataclass(slots=True, frozen=True)
class GuardVerdict:
    kind: Literal["allow", "ignore", "refuse"]
    reason: str = ""
    refusal_text: str = ""

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class TurnGuards:
    """Loop safety, rate limits, cooldowns, and input caps for one worker."""

    counters: CounterBackend
    history: RecentTexts
    user_per_minute: int = 20
    chat_per_minute: int = 30
    group_cooldown_seconds: float = 3.0
    input_max_chars: int = 4_000
    loop_guard_enabled: bool = True
    key_prefix: str = "mybot:guard"
    now: Callable[[], datetime] = field(default=_utc_now)

    async def check(
        self,
        event: InboundEvent,
        decision: TurnDecision,
        *,
        conversation_id: UUID,
        stable_key: str,
    ) -> GuardVerdict:
        envelope = event.envelope
        if self.loop_guard_enabled and event.sender_is_bot:
            return GuardVerdict(kind="ignore", reason="sender is a bot")
        if self.loop_guard_enabled and await self._is_echo(conversation_id, envelope):
            return GuardVerdict(kind="ignore", reason="inbound text mirrors a recent reply")
        # Operational commands always pass rate limiting so /status stays usable.
        if decision.trigger is TurnTrigger.COMMAND:
            return GuardVerdict(kind="allow")

        minute = self.now().strftime("%Y%m%dT%H%M")
        user_verdict = await self._counter_verdict(
            f"{self.key_prefix}:user:{envelope.sender_identity_id}:{minute}",
            self.user_per_minute,
            "per-user rate limit",
        )
        if user_verdict is not None:
            return user_verdict
        chat_verdict = await self._counter_verdict(
            f"{self.key_prefix}:chat:{stable_key}:{minute}",
            self.chat_per_minute,
            "per-chat rate limit",
        )
        if chat_verdict is not None:
            return chat_verdict

        if decision.action is TurnAction.AGENT:
            if len(envelope_text(envelope)) > self.input_max_chars:
                return GuardVerdict(
                    kind="refuse",
                    reason="input over the size cap",
                    refusal_text=INPUT_TOO_LONG_REFUSAL,
                )
            if (
                envelope.chat_kind is not ChatKind.DIRECT
                and self.group_cooldown_seconds > 0
            ):
                fresh = await self.counters.acquire_once(
                    f"{self.key_prefix}:cooldown:{stable_key}",
                    ttl_seconds=max(1, int(self.group_cooldown_seconds)),
                )
                if not fresh:
                    return GuardVerdict(
                        kind="ignore", reason="group cooldown is active"
                    )
        return GuardVerdict(kind="allow")

    async def _counter_verdict(
        self, key: str, limit: int, label: str
    ) -> GuardVerdict | None:
        if limit <= 0:
            return None
        count = await self.counters.increment(key, 1, ttl_seconds=120)
        if count <= limit:
            return None
        if count == limit + 1:
            logger.warning("guard_rate_limited", key=key, label=label)
            return GuardVerdict(
                kind="refuse", reason=label, refusal_text=RATE_LIMIT_REFUSAL
            )
        return GuardVerdict(kind="ignore", reason=f"{label} exceeded")

    async def _is_echo(self, conversation_id: UUID, envelope: object) -> bool:
        from mybot.contracts import MessageEnvelope

        assert isinstance(envelope, MessageEnvelope)
        inbound = envelope_text(envelope).strip()
        if not inbound:
            return False
        rows = await self.history.recent_texts(conversation_id, limit=_ECHO_WINDOW * 2)
        recent_outbound = [
            row.text.strip() for row in rows if row.direction == "outbound"
        ][:_ECHO_WINDOW]
        return inbound in recent_outbound
