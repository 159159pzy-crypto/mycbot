from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
)
from mybot.engine.personality_learning import PersonalityLearningJob
from mybot.infrastructure.llm import ChatMessage, LlmReply
from mybot.infrastructure.streams import MemoryStreamBackend
from mybot.repositories.memory import MemoryRecord, MergeApplication
from mybot.repositories.messages import GroupLearningContext, MessageText


@dataclass
class FakeSource:
    contexts: tuple[GroupLearningContext, ...]

    async def recent_group_learning_contexts(self, **kwargs):  # type: ignore[no-untyped-def]
        return self.contexts


@dataclass
class FakeLlm:
    text: str
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return LlmReply(
            text=self.text,
            model="memory-small",
            prompt_tokens=10,
            completion_tokens=5,
        )


@dataclass
class FakeMemory:
    items: list[MemoryItem] = field(default_factory=list)

    async def merge_item(self, item: MemoryItem, *, source: str):
        self.items.append(item)
        return MergeApplication(MemoryOperation.ADD, item.id, None, True), (0, 0)


@dataclass
class FakeRelationships:
    current: dict[str, MemoryRecord] = field(default_factory=dict)
    items: list[MemoryItem] = field(default_factory=list)

    async def relationship_for(self, subject_identity_id: str, *, now: datetime):
        return self.current.get(subject_identity_id)

    async def upsert_relationship(self, item: MemoryItem, *, source: str, now: datetime):
        self.items.append(item)
        return MergeApplication(MemoryOperation.ADD, item.id, None, True)


def context() -> GroupLearningContext:
    return GroupLearningContext(
        conversation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="qq-main",
            chat_kind=ChatKind.GROUP,
            chat_id="7788",
        ),
        source_message_ids=("m1", "m2", "m3"),
        messages=(
            MessageText(id=uuid4(), direction="inbound", sender="qq:1", text="绝绝子"),
            MessageText(id=uuid4(), direction="inbound", sender="qq:2", text="确实, 笑死"),
            MessageText(id=uuid4(), direction="inbound", sender="qq:1", text="这波可以"),
        ),
    )


@pytest.mark.asyncio
async def test_group_learning_keeps_expression_local_and_versions_relationship() -> None:
    llm = FakeLlm(
        text=(
            '{"expressions":[{"content":"短句, 轻松收尾","confidence":0.9}],'
            '"relationships":[{"subject_identity_id":"qq:1",'
            '"impression":"交流直接, 喜欢轻松回应","familiarity_delta":3.5}]}'
        )
    )
    memory = FakeMemory()
    relationships = FakeRelationships(
        current={
            "qq:1": MemoryRecord(
                id=uuid4(),
                scope=MemoryScope.SUBJECT,
                subject_identity_id="qq:1",
                conversation_stable_key=None,
                privacy=MemoryPrivacy.PRIVATE,
                kind="RELATIONSHIP",
                content="初次交流",
                confidence=0.8,
                source_message_ids=("old",),
                created_at=datetime(2026, 7, 27, tzinfo=UTC),
                relationship_score=10.0,
            )
        }
    )
    job = PersonalityLearningJob(
        source=FakeSource((context(),)),
        memory=memory,
        relationships=relationships,
        llm=llm,
        once=MemoryStreamBackend(),
        enabled=True,
        now=lambda: datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert await job.run_pass() == 2
    expression = memory.items[0]
    assert expression.scope.value == "CONVERSATION"
    assert expression.privacy.value == "SHARED"
    assert expression.kind == "EXPRESSION"
    assert expression.conversation == context().conversation
    relationship = relationships.items[0]
    assert relationship.scope.value == "SUBJECT"
    assert relationship.privacy.value == "PRIVATE"
    assert relationship.relationship_score == 13.5


@pytest.mark.asyncio
async def test_learning_rejects_low_confidence_and_unknown_relationship_subjects() -> None:
    llm = FakeLlm(
        text=(
            '{"expressions":[{"content":"不可靠","confidence":0.2}],'
            '"relationships":[{"subject_identity_id":"qq:outsider",'
            '"impression":"不应保存","familiarity_delta":5}]}'
        )
    )
    memory = FakeMemory()
    relationships = FakeRelationships()
    job = PersonalityLearningJob(
        source=FakeSource((context(),)),
        memory=memory,
        relationships=relationships,
        llm=llm,
        once=MemoryStreamBackend(),
        enabled=True,
    )

    assert await job.run_pass() == 0
    assert memory.items == []
    assert relationships.items == []


@pytest.mark.asyncio
async def test_learning_fingerprint_prevents_reprocessing_same_dialogue() -> None:
    backend = MemoryStreamBackend()
    llm = FakeLlm(text='{"expressions":[],"relationships":[]}')
    job = PersonalityLearningJob(
        source=FakeSource((context(),)),
        memory=FakeMemory(),
        relationships=FakeRelationships(),
        llm=llm,
        once=backend,
        enabled=True,
    )

    await job.run_pass()
    await job.run_pass()

    assert len(llm.calls) == 1
