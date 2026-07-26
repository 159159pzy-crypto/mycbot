from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    MessageEnvelope,
    Platform,
    TextSegment,
)
from mybot.engine.memory_service import MemoryService
from mybot.infrastructure.embeddings import EmbeddingError
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.repositories.memory import ScoredMemory

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


@dataclass
class FakeEmbeddings:
    fail: bool = False
    requests: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.requests.append(list(texts))
        if self.fail:
            raise EmbeddingError("down", retryable=True)
        return [[0.1, 0.2] for _ in texts]


@dataclass
class FakeStore:
    results: tuple[ScoredMemory, ...] = ()
    stored: list[tuple[MemoryItem, list[float] | None, str | None]] = field(
        default_factory=list
    )
    searches: list[dict[str, object]] = field(default_factory=list)
    revoked: list[dict[str, object]] = field(default_factory=list)

    async def store(self, item, *, embedding, embedding_model):  # type: ignore[no-untyped-def]
        self.stored.append((item, list(embedding) if embedding else None, embedding_model))
        return item.id

    async def search(self, **kwargs):  # type: ignore[no-untyped-def]
        self.searches.append(kwargs)
        return self.results

    async def revoke_for(self, **kwargs):  # type: ignore[no-untyped-def]
        self.revoked.append(kwargs)
        return 4


@dataclass
class ScriptedLlm:
    reply: LlmReply | None = None
    error: LlmError | None = None
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(self, messages, *, tools=None):  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        assert self.reply is not None
        return self.reply


def envelope(
    *, chat_kind: ChatKind = ChatKind.DIRECT, text: str = "我喜欢美式咖啡"
) -> MessageEnvelope:
    return MessageEnvelope(
        id="telegram:telegram-main:777:88",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=chat_kind,
        chat_id="777" if chat_kind is ChatKind.DIRECT else "-1001",
        sender_identity_id="telegram:777",
        occurred_at=NOW,
        segments=(TextSegment(text=text),),
    )


def scored(content: str, *, distance: float = 0.1, memory_id: UUID | None = None) -> ScoredMemory:
    return ScoredMemory(
        id=memory_id or uuid4(),
        scope=MemoryScope.SUBJECT,
        privacy=MemoryPrivacy.PRIVATE,
        kind="preference",
        content=content,
        confidence=0.9,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        distance=distance,
        supersedes=(),
    )


def make_service(
    *,
    store: FakeStore | None = None,
    embeddings: FakeEmbeddings | None = None,
    llm: ScriptedLlm | None = None,
    token_budget: int = 1_200,
) -> tuple[MemoryService, FakeStore, FakeEmbeddings]:
    resolved_store = store or FakeStore()
    resolved_embeddings = embeddings or FakeEmbeddings()
    service = MemoryService(
        embeddings=resolved_embeddings,  # type: ignore[arg-type]
        store=resolved_store,
        llm=llm,
        embedding_model="test-embed",
        min_confidence=0.6,
        retrieval_limit=5,
        token_budget=token_budget,
        now=lambda: NOW,
    )
    return service, resolved_store, resolved_embeddings


@pytest.mark.asyncio
async def test_retrieval_block_renders_provenance_and_scope_flags() -> None:
    store = FakeStore(results=(scored("喜欢美式咖啡"), scored("住在上海", distance=0.3)))
    service, _, embeddings = make_service(store=store)

    block = await service.retrieval_block(envelope(), "咖啡怎么选")

    assert block is not None
    assert "[subject|2026-07-01] 喜欢美式咖啡" in block
    assert "住在上海" in block
    assert embeddings.requests == [["咖啡怎么选"]]
    search = store.searches[0]
    assert search["include_private"] is True
    assert search["subject_identity_id"] == "telegram:777"


@pytest.mark.asyncio
async def test_group_retrieval_never_includes_private_memories() -> None:
    store = FakeStore(results=())
    service, _, _ = make_service(store=store)

    await service.retrieval_block(envelope(chat_kind=ChatKind.GROUP), "hello")

    assert store.searches[0]["include_private"] is False


@pytest.mark.asyncio
async def test_token_budget_keeps_best_lines_first() -> None:
    store = FakeStore(
        results=tuple(
            scored(f"memory number {index} " + "x" * 200, distance=index / 10)
            for index in range(10)
        )
    )
    service, _, _ = make_service(store=store, token_budget=200)

    block = await service.retrieval_block(envelope(), "hi")

    assert block is not None
    assert "memory number 0" in block
    assert "memory number 9" not in block


@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_no_block() -> None:
    service, store, _ = make_service(embeddings=FakeEmbeddings(fail=True))

    assert await service.retrieval_block(envelope(), "hi") is None
    assert store.searches == []


@pytest.mark.asyncio
async def test_no_memories_yields_no_block() -> None:
    service, _, _ = make_service(store=FakeStore(results=()))

    assert await service.retrieval_block(envelope(), "hi") is None


@pytest.mark.asyncio
async def test_extraction_stores_policy_filtered_candidates() -> None:
    llm = ScriptedLlm(
        reply=LlmReply(
            text=(
                '[{"content": "用户喜欢美式咖啡", "kind": "preference", '
                '"scope": "subject", "confidence": 0.9},'
                '{"content": "这个群在讨论周末聚餐", "kind": "context", '
                '"scope": "global", "confidence": 0.8},'
                '{"content": "低置信度杂音", "kind": "noise", '
                '"scope": "subject", "confidence": 0.3}]'
            ),
            model="deepseek-chat-v3",
            prompt_tokens=80,
            completion_tokens=40,
        )
    )
    service, store, _ = make_service(llm=llm)

    outcome = await service.extract_and_store(envelope(), "好的,记住了。")

    assert outcome.stored == 2
    assert (outcome.prompt_tokens, outcome.completion_tokens) == (80, 40)
    first_item = store.stored[0][0]
    assert first_item.scope is MemoryScope.SUBJECT
    assert first_item.subject_identity_id == "telegram:777"
    assert first_item.privacy is MemoryPrivacy.PRIVATE
    assert first_item.source_message_ids == ("telegram:telegram-main:777:88",)
    second_item = store.stored[1][0]  # "global" downgraded to conversation scope
    assert second_item.scope is MemoryScope.CONVERSATION
    assert second_item.privacy is MemoryPrivacy.SHARED
    assert second_item.conversation is not None
    assert all(item.privacy is not MemoryPrivacy.SENSITIVE for item, _, _ in store.stored)


@pytest.mark.asyncio
async def test_extraction_tolerates_prose_wrapped_json_and_junk() -> None:
    wrapped = ScriptedLlm(
        reply=LlmReply(
            text='好的,提取结果如下:\n[{"content": "在上海工作", "kind": "fact", '
            '"scope": "subject", "confidence": 0.8}]\n以上。',
            model="m",
            prompt_tokens=10,
            completion_tokens=5,
        )
    )
    service, store, _ = make_service(llm=wrapped)
    outcome = await service.extract_and_store(envelope(), "回复")
    assert outcome.stored == 1
    assert store.stored[0][0].content == "在上海工作"

    junk = ScriptedLlm(
        reply=LlmReply(
            text="nothing to extract here", model="m", prompt_tokens=1, completion_tokens=1
        )
    )
    service_junk, store_junk, _ = make_service(llm=junk)
    outcome_junk = await service_junk.extract_and_store(envelope(), "回复")
    assert outcome_junk.stored == 0
    assert store_junk.stored == []


@pytest.mark.asyncio
async def test_extraction_llm_failure_is_swallowed() -> None:
    service, store, _ = make_service(
        llm=ScriptedLlm(error=LlmError("down", retryable=True))
    )

    outcome = await service.extract_and_store(envelope(), "回复")

    assert outcome.stored == 0
    assert store.stored == []


@pytest.mark.asyncio
async def test_forget_delegates_with_clock() -> None:
    service, store, _ = make_service()

    count = await service.forget(
        subject_identity_id="telegram:777",
        conversation_stable_key="v1:telegram-main:DIRECT:777:0",
    )

    assert count == 4
    assert store.revoked[0]["subject_identity_id"] == "telegram:777"
    assert store.revoked[0]["now"] == NOW
