from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from mybot.adapters import OutboundMessage
from mybot.contracts import AgentProfile, PersonaVersion, Platform, ReplyPlan, TurnDecision
from mybot.infrastructure.llm import ChatMessage, LlmReply
from mybot.infrastructure.streams import MemoryStreamBackend
from mybot.repositories.conversations import ConversationDetail
from mybot.repositories.messages import MessageText
from mybot.repositories.profiles import ResolvedProfile
from mybot.repositories.turns import TurnOutcome
from mybot.services.proactive import ProactivePass, in_quiet_hours


@dataclass
class FakeConfig:
    optin: list[str]

    async def get(self, key: str) -> JsonValue | None:
        return list(self.optin)


@dataclass
class FakeLookup:
    known: dict[str, ConversationDetail]

    async def by_stable_key(self, stable_key: str) -> ConversationDetail | None:
        return self.known.get(stable_key)


@dataclass
class FakeMessages:
    outbound: list[tuple[UUID, ReplyPlan]] = field(default_factory=list)

    async def record_outbound(  # type: ignore[no-untyped-def]
        self, conversation_id, plan, *, occurred_at=None, trace_id=None
    ):
        self.outbound.append((conversation_id, plan))
        return uuid4()


@dataclass
class FakeTurns:
    recorded: list[tuple[UUID, TurnDecision, TurnOutcome]] = field(default_factory=list)

    async def record_turn(self, conversation_id, decision, *, outcome, **kwargs):  # type: ignore[no-untyped-def]
        self.recorded.append((conversation_id, decision, outcome))
        return uuid4()


@dataclass
class FakePublisher:
    payloads: list[str] = field(default_factory=list)

    async def publish(self, payload: str) -> str:
        self.payloads.append(payload)
        return "1-0"


@dataclass
class FakeHistory:
    rows: tuple[MessageText, ...]

    async def recent_texts(self, conversation_id: UUID, *, limit: int = 40):
        return self.rows[:limit]


@dataclass
class FakeLlm:
    text: str
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return LlmReply(
            text=self.text,
            model="heartbeat-small",
            prompt_tokens=20,
            completion_tokens=6,
        )


@dataclass
class FakeProfiles:
    resolved: ResolvedProfile

    async def resolve(self, conversation_id: UUID, **kwargs):  # type: ignore[no-untyped-def]
        return self.resolved


@dataclass
class FakeAudit:
    rows: list[dict[str, object]] = field(default_factory=list)

    async def record(self, **kwargs):  # type: ignore[no-untyped-def]
        self.rows.append(kwargs)


def detail(stable_key: str = "conv-a") -> ConversationDetail:
    return ConversationDetail(
        id=uuid4(),
        stable_key=stable_key,
        connection_id="telegram-main",
        platform="TELEGRAM",
        chat_kind="DIRECT",
        chat_id="777",
    )


def make_pass(
    *,
    optin: list[str],
    known: dict[str, ConversationDetail],
    enabled: bool = True,
    hour: int = 12,
    backend: MemoryStreamBackend | None = None,
) -> tuple[ProactivePass, FakeMessages, FakeTurns, FakePublisher]:
    messages = FakeMessages()
    turns = FakeTurns()
    publisher = FakePublisher()
    proactive = ProactivePass(
        config=FakeConfig(optin=optin),
        conversations=FakeLookup(known=known),
        messages=messages,
        turns=turns,
        outbound=publisher,
        once=backend or MemoryStreamBackend(),
        enabled=enabled,
        message="好久没聊了,最近怎么样?",
        min_interval_hours=24.0,
        quiet_start_hour=22,
        quiet_end_hour=8,
        now=lambda: datetime(2026, 7, 26, hour, tzinfo=UTC),
    )
    return proactive, messages, turns, publisher


def test_quiet_hours_wrap_midnight_and_equal_bounds_disable() -> None:
    assert in_quiet_hours(23, 22, 8) is True
    assert in_quiet_hours(3, 22, 8) is True
    assert in_quiet_hours(12, 22, 8) is False
    assert in_quiet_hours(10, 9, 17) is True
    assert in_quiet_hours(8, 9, 17) is False
    assert in_quiet_hours(5, 6, 6) is False


@pytest.mark.asyncio
async def test_sends_template_checkin_and_records_a_proactive_turn() -> None:
    conversation = detail()
    proactive, messages, turns, publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}
    )

    sent = await proactive.run_pass()

    assert sent == 1
    outbound = OutboundMessage.model_validate_json(publisher.payloads[0])
    assert outbound.platform is Platform.TELEGRAM
    assert outbound.chat_id == "777"
    assert outbound.reply_plan.text_segments == ("好久没聊了,最近怎么样?",)
    assert messages.outbound[0][0] == conversation.id
    _, decision, outcome = turns.recorded[0]
    assert decision.trigger.value == "PROACTIVE"
    assert outcome == "replied"


@pytest.mark.asyncio
async def test_frequency_cap_blocks_repeat_sends_within_the_interval() -> None:
    conversation = detail()
    backend = MemoryStreamBackend()
    proactive, _, _, publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}, backend=backend
    )

    first = await proactive.run_pass()
    second = await proactive.run_pass()

    assert (first, second) == (1, 0)
    assert len(publisher.payloads) == 1


@pytest.mark.asyncio
async def test_quiet_hours_and_kill_switch_block_sending() -> None:
    conversation = detail()

    quiet, _, _, quiet_publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}, hour=23
    )
    disabled, _, _, disabled_publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}, enabled=False
    )

    assert await quiet.run_pass() == 0
    assert await disabled.run_pass() == 0
    assert quiet_publisher.payloads == []
    assert disabled_publisher.payloads == []


@pytest.mark.asyncio
async def test_only_opted_in_and_known_conversations_receive_checkins() -> None:
    conversation = detail()
    proactive, _, _, publisher = make_pass(
        optin=["conv-a", "ghost-key", "  "], known={"conv-a": conversation}
    )

    sent = await proactive.run_pass()

    assert sent == 1
    assert len(publisher.payloads) == 1


@pytest.mark.asyncio
async def test_llm_heartbeat_uses_profile_and_recent_topic_then_audits_send() -> None:
    conversation = detail()
    proactive, messages, turns, publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}
    )
    profile_id = uuid4()
    persona = PersonaVersion(
        profile_id=profile_id,
        version=2,
        system_prompt="温柔但克制地关心对方",
    )
    proactive.profiles = FakeProfiles(
        ResolvedProfile(
            profile=AgentProfile(
                id=profile_id,
                name="私聊陪伴",
                active_persona_version_id=persona.id,
                model_tier="economy",
            ),
            persona=persona,
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            updated_at=datetime(2026, 7, 28, tzinfo=UTC),
        )
    )
    proactive.history = FakeHistory(
        (
            MessageText(direction="inbound", sender="telegram:777", text="明天要面试"),
            MessageText(direction="outbound", sender="self", text="祝你顺利"),
        )
    )
    llm = FakeLlm("明天面试加油, 记得早点休息.")
    audit = FakeAudit()
    proactive.llm = llm
    proactive.audit = audit

    assert await proactive.run_pass() == 1
    assert "明天要面试" in str(llm.calls[0][-1].content)
    assert "温柔但克制" in str(llm.calls[0][0].content)
    assert messages.outbound[0][1].text_segments == ("明天面试加油, 记得早点休息.",)
    assert audit.rows[0]["outcome"] == "sent"
    assert audit.rows[0]["profile_id"] == profile_id
    assert turns.recorded[0][2] == "replied"
    assert len(publisher.payloads) == 1


@pytest.mark.asyncio
async def test_heartbeat_ok_is_silently_suppressed_and_audited() -> None:
    conversation = detail()
    proactive, messages, turns, publisher = make_pass(
        optin=["conv-a"], known={"conv-a": conversation}
    )
    proactive.history = FakeHistory(())
    proactive.llm = FakeLlm("  HEARTBEAT_OK  ")
    audit = FakeAudit()
    proactive.audit = audit

    assert await proactive.run_pass() == 0
    assert messages.outbound == []
    assert turns.recorded == []
    assert publisher.payloads == []
    assert audit.rows[0]["outcome"] == "suppressed"
