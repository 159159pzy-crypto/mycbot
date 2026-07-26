from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mybot.adapters import InboundEvent
from mybot.contracts import (
    ChatKind,
    MessageEnvelope,
    Platform,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.engine.guards import (
    INPUT_TOO_LONG_REFUSAL,
    RATE_LIMIT_REFUSAL,
    TurnGuards,
)
from mybot.infrastructure.streams import MemoryStreamBackend
from mybot.repositories.messages import MessageText

NOW = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)
CONVERSATION_ID = uuid4()


@dataclass
class FakeHistory:
    rows: tuple[MessageText, ...] = ()
    calls: list[UUID] = field(default_factory=list)

    async def recent_texts(
        self, conversation_id: UUID, *, limit: int = 40
    ) -> tuple[MessageText, ...]:
        self.calls.append(conversation_id)
        return self.rows[:limit]


def envelope(
    *,
    text: str = "hello",
    chat_kind: ChatKind = ChatKind.DIRECT,
    sender: str = "telegram:777",
) -> MessageEnvelope:
    return MessageEnvelope(
        id=f"telegram:telegram-main:777:{uuid4().hex[:6]}",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=chat_kind,
        chat_id="777" if chat_kind is ChatKind.DIRECT else "-1001",
        sender_identity_id=sender,
        occurred_at=NOW,
        segments=(TextSegment(text=text),),
    )


def event(
    *,
    text: str = "hello",
    chat_kind: ChatKind = ChatKind.DIRECT,
    sender_is_bot: bool = False,
    sender: str = "telegram:777",
) -> InboundEvent:
    return InboundEvent(
        envelope=envelope(text=text, chat_kind=chat_kind, sender=sender),
        sender_is_bot=sender_is_bot,
    )


def decision(
    *,
    action: TurnAction = TurnAction.AGENT,
    trigger: TurnTrigger = TurnTrigger.DIRECT_MESSAGE,
) -> TurnDecision:
    return TurnDecision(action=action, reason="test", confidence=1.0, trigger=trigger)


def make_guards(
    *,
    backend: MemoryStreamBackend | None = None,
    history: FakeHistory | None = None,
    user_limit: int = 3,
    chat_limit: int = 100,
    cooldown: float = 3.0,
    input_cap: int = 100,
) -> TurnGuards:
    return TurnGuards(
        counters=backend or MemoryStreamBackend(),
        history=history or FakeHistory(),
        user_per_minute=user_limit,
        chat_per_minute=chat_limit,
        group_cooldown_seconds=cooldown,
        input_max_chars=input_cap,
        now=lambda: NOW,
    )


async def check(guards: TurnGuards, current: InboundEvent, verdict_decision: TurnDecision):  # type: ignore[no-untyped-def]
    return await guards.check(
        current,
        verdict_decision,
        conversation_id=CONVERSATION_ID,
        stable_key="v1:telegram-main:DIRECT:777:0",
    )


@pytest.mark.asyncio
async def test_bot_senders_are_ignored() -> None:
    verdict = await check(make_guards(), event(sender_is_bot=True), decision())

    assert verdict.kind == "ignore"
    assert "bot" in verdict.reason


@pytest.mark.asyncio
async def test_echoed_reply_text_is_ignored_to_break_loops() -> None:
    history = FakeHistory(
        rows=(
            MessageText(direction="outbound", sender="self", text="pong"),
            MessageText(direction="inbound", sender="telegram:777", text="hi"),
        )
    )
    guards = make_guards(history=history)

    verdict = await check(guards, event(text="pong"), decision())
    fresh = await check(guards, event(text="something new"), decision())

    assert verdict.kind == "ignore"
    assert fresh.kind == "allow"


@pytest.mark.asyncio
async def test_user_rate_limit_refuses_once_then_goes_silent() -> None:
    guards = make_guards(user_limit=2)

    verdicts = [await check(guards, event(), decision()) for _ in range(5)]

    assert [verdict.kind for verdict in verdicts] == [
        "allow",
        "allow",
        "refuse",
        "ignore",
        "ignore",
    ]
    assert verdicts[2].refusal_text == RATE_LIMIT_REFUSAL


@pytest.mark.asyncio
async def test_chat_rate_limit_covers_all_senders() -> None:
    guards = make_guards(user_limit=0, chat_limit=1)  # user limit disabled

    first = await check(guards, event(sender="telegram:1"), decision())
    second = await check(guards, event(sender="telegram:2"), decision())

    assert first.kind == "allow"
    assert second.kind == "refuse"


@pytest.mark.asyncio
async def test_commands_bypass_rate_limits() -> None:
    guards = make_guards(user_limit=1)
    await check(guards, event(), decision())  # consume the single slot

    verdict = await check(
        guards,
        event(text="/status"),
        decision(action=TurnAction.DIRECT_REPLY, trigger=TurnTrigger.COMMAND),
    )

    assert verdict.kind == "allow"


@pytest.mark.asyncio
async def test_oversized_agent_input_is_refused_deterministically() -> None:
    guards = make_guards(input_cap=10)

    verdict = await check(guards, event(text="x" * 50), decision())

    assert verdict.kind == "refuse"
    assert verdict.refusal_text == INPUT_TOO_LONG_REFUSAL


@pytest.mark.asyncio
async def test_group_cooldown_ignores_rapid_followups_but_not_direct_chats() -> None:
    backend = MemoryStreamBackend()
    guards = make_guards(backend=backend, user_limit=100)
    group = event(chat_kind=ChatKind.GROUP)
    group_decision = decision(trigger=TurnTrigger.MENTION)

    first = await guards.check(
        group, group_decision, conversation_id=CONVERSATION_ID, stable_key="group-key"
    )
    second = await guards.check(
        group, group_decision, conversation_id=CONVERSATION_ID, stable_key="group-key"
    )
    direct_one = await check(guards, event(), decision())
    direct_two = await check(guards, event(), decision())

    assert first.kind == "allow"
    assert second.kind == "ignore"
    assert direct_one.kind == "allow"
    assert direct_two.kind == "allow"


@pytest.mark.asyncio
async def test_zero_limits_disable_the_counters() -> None:
    guards = make_guards(user_limit=0, chat_limit=0)

    verdicts = [await check(guards, event(), decision()) for _ in range(10)]

    assert all(verdict.kind == "allow" for verdict in verdicts)
