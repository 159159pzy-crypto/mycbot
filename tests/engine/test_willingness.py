from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    MessageEnvelope,
    Platform,
    ReplyWillingnessPolicy,
    TextSegment,
    WillingnessScore,
)
from mybot.engine.willingness import ActivitySnapshot, ReplyWillingnessScorer


@dataclass
class FakeActivity:
    snapshot: ActivitySnapshot

    async def recent_activity(self, conversation_id, *, since, limit=200):  # type: ignore[no-untyped-def]
        return self.snapshot


@dataclass
class FakeSemantic:
    persona: float = 0.0
    memory: float = 0.0
    fail: bool = False

    async def participation_relevance(self, envelope, text, persona):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return self.persona, self.memory


@dataclass
class FakeAudit:
    scores: list[WillingnessScore] = field(default_factory=list)

    async def record(self, **kwargs):  # type: ignore[no-untyped-def]
        self.scores.append(kwargs["score"])


def envelope(text: str) -> MessageEnvelope:
    return MessageEnvelope(
        id="qq:main:group:1",
        connection_id="qq-main",
        platform=Platform.QQ,
        chat_kind=ChatKind.GROUP,
        chat_id="7788",
        sender_identity_id="qq:7",
        occurred_at=datetime(2026, 7, 28, tzinfo=UTC),
        segments=(TextSegment(text=text),),
    )


@pytest.mark.asyncio
async def test_relevant_question_crosses_conservative_threshold() -> None:
    audit = FakeAudit()
    scorer = ReplyWillingnessScorer(
        activity=FakeActivity(ActivitySnapshot(inbound=14, outbound=1)),
        semantic=FakeSemantic(persona=0.8, memory=0.9),
        audit=audit,
        now=lambda: datetime(2026, 7, 28, tzinfo=UTC),
    )

    score = await scorer.evaluate(
        envelope=envelope("麦麦, 这个异步问题应该怎么排查?"),
        conversation_id=uuid4(),
        message_id=uuid4(),
        profile_id=uuid4(),
        persona="你擅长工程排障",
        policy=ReplyWillingnessPolicy(
            enabled=True,
            threshold=0.7,
            keywords=("麦麦",),
        ),
    )

    assert score.allowed is True
    assert score.components.keyword == 1.0
    assert score.components.question == 1.0
    assert audit.scores == [score]


@pytest.mark.asyncio
async def test_recent_bot_presence_keeps_weak_group_chatter_silent() -> None:
    scorer = ReplyWillingnessScorer(
        activity=FakeActivity(ActivitySnapshot(inbound=5, outbound=5)),
        semantic=FakeSemantic(persona=0.2, memory=0.1),
        audit=FakeAudit(),
        now=lambda: datetime(2026, 7, 28, tzinfo=UTC),
    )

    score = await scorer.evaluate(
        envelope=envelope("哈哈"),
        conversation_id=uuid4(),
        message_id=uuid4(),
        profile_id=uuid4(),
        persona="友好助手",
        policy=ReplyWillingnessPolicy(enabled=True, threshold=0.7),
    )

    assert score.allowed is False
    assert score.components.presence_penalty == 0.5


@pytest.mark.asyncio
async def test_semantic_failure_fails_closed_for_unsolicited_turns() -> None:
    audit = FakeAudit()
    scorer = ReplyWillingnessScorer(
        activity=FakeActivity(ActivitySnapshot(inbound=20, outbound=0)),
        semantic=FakeSemantic(fail=True),
        audit=audit,
    )

    score = await scorer.evaluate(
        envelope=envelope("这是个问题吗?"),
        conversation_id=uuid4(),
        message_id=uuid4(),
        profile_id=uuid4(),
        persona="助手",
        policy=ReplyWillingnessPolicy(enabled=True, threshold=0.5),
    )

    assert score.allowed is False
    assert score.score == 0
    assert score.reason == "semantic relevance unavailable"
    assert audit.scores == [score]
