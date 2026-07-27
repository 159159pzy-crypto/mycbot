from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mybot.contracts import ChatKind, ConversationKey, MemoryItem, MemoryOperation
from mybot.engine.memory_consolidation import MemoryConsolidationJob
from mybot.infrastructure.llm import LlmReply
from mybot.repositories.memory import ConsolidationContext, MergeApplication

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


@dataclass
class FakeSource:
    contexts: tuple[ConsolidationContext, ...]
    calls: list[dict[str, object]] = field(default_factory=list)

    async def recent_consolidation_contexts(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return self.contexts


@dataclass
class FakeMemory:
    items: list[tuple[MemoryItem, str]] = field(default_factory=list)

    async def merge_item(self, item: MemoryItem, *, source: str):  # type: ignore[no-untyped-def]
        self.items.append((item, source))
        return MergeApplication(MemoryOperation.ADD, item.id, None, True), (0, 0)


@dataclass
class FakeLlm:
    reply: LlmReply

    async def complete(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        return self.reply


@dataclass
class FakeOnce:
    acquired: bool = True
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool:
        self.calls.append((key, ttl_seconds))
        return self.acquired


def consolidation_context() -> ConsolidationContext:
    return ConsolidationContext(
        conversation=ConversationKey(
            connection_id="telegram-main",
            chat_kind=ChatKind.DIRECT,
            chat_id="777",
        ),
        subject_identity_id="telegram:777",
        source_message_ids=(str(uuid4()),),
        transcript=("inbound:telegram:777: 我最近改喝无糖美式",),
        memories=(),
    )


@pytest.mark.asyncio
async def test_consolidation_promotes_candidates_through_normal_merge_pipeline() -> None:
    source = FakeSource((consolidation_context(),))
    memory = FakeMemory()
    once = FakeOnce()
    job = MemoryConsolidationJob(
        source=source,
        memory=memory,
        llm=FakeLlm(
            LlmReply(
                text=(
                    '[{"content":"用户现在偏好无糖美式","kind":"preference",'
                    '"scope":"subject","confidence":0.9},'
                    '{"content":"低置信度猜测","kind":"noise",'
                    '"scope":"subject","confidence":0.2}]'
                ),
                model="memory-model",
                prompt_tokens=30,
                completion_tokens=10,
            )
        ),
        once=once,
        now=lambda: NOW,
    )

    applied = await job.run_pass()

    assert applied == 1
    assert memory.items[0][0].subject_identity_id == "telegram:777"
    assert memory.items[0][1] == "sleep_consolidation"
    assert once.calls[0][0].startswith("mybot:memory-consolidation:")


@pytest.mark.asyncio
async def test_consolidation_debounce_skips_model_and_source_work() -> None:
    source = FakeSource((consolidation_context(),))
    memory = FakeMemory()
    job = MemoryConsolidationJob(
        source=source,
        memory=memory,
        llm=FakeLlm(LlmReply(text="[]", model="m", prompt_tokens=0, completion_tokens=0)),
        once=FakeOnce(acquired=False),
        now=lambda: NOW,
    )

    assert await job.run_pass() == 0
    assert source.calls == []
    assert memory.items == []
