import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryMergeDecision,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
)
from mybot.repositories.memory import MemoryRepository

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="MYBOT_TEST_DATABASE_URL is not configured"),
]

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)
MODEL = "test-embed"
SUBJECT = "telegram:777"
OTHER_SUBJECT = "telegram:999"
CONVERSATION = ConversationKey(
    connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
)
OTHER_CONVERSATION = ConversationKey(
    connection_id="telegram-main", chat_kind=ChatKind.GROUP, chat_id="-1001"
)


@pytest.fixture(scope="module")
def migrated_database_url() -> str:
    assert DATABASE_URL is not None
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "MYBOT_DATABASE_URL": DATABASE_URL},
        check=True,
        capture_output=True,
    )
    return DATABASE_URL


@pytest.fixture
async def sessions(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(sa.text("TRUNCATE memory_items"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def item(
    *,
    content: str,
    scope: MemoryScope = MemoryScope.SUBJECT,
    subject: str | None = SUBJECT,
    conversation: ConversationKey | None = None,
    privacy: MemoryPrivacy = MemoryPrivacy.PRIVATE,
    confidence: float = 0.9,
    valid_until: datetime | None = None,
    supersedes: tuple = (),
) -> MemoryItem:
    if scope is MemoryScope.SUBJECT:
        return MemoryItem(
            scope=scope,
            subject_identity_id=subject,
            kind="preference",
            content=content,
            source_message_ids=("telegram:telegram-main:777:1",),
            confidence=confidence,
            privacy=privacy,
            valid_until=valid_until,
            supersedes=supersedes,
        )
    if scope is MemoryScope.CONVERSATION:
        return MemoryItem(
            scope=scope,
            conversation=conversation or CONVERSATION,
            kind="context",
            content=content,
            source_message_ids=("telegram:telegram-main:777:1",),
            confidence=confidence,
            privacy=privacy,
            valid_until=valid_until,
            supersedes=supersedes,
        )
    return MemoryItem(
        scope=scope,
        kind="fact",
        content=content,
        source_message_ids=("telegram:telegram-main:777:1",),
        confidence=confidence,
        privacy=privacy,
        valid_until=valid_until,
        supersedes=supersedes,
    )


async def search(
    repository: MemoryRepository,
    *,
    query: list[float],
    query_text: str = "咖啡",
    include_private: bool = True,
    subject: str = SUBJECT,
    conversation_key: str | None = None,
    now: datetime = NOW,
):  # type: ignore[no-untyped-def]
    return await repository.search(
        query_embedding=query,
        query_text=query_text,
        embedding_model=MODEL,
        subject_identity_id=subject,
        conversation_stable_key=conversation_key or CONVERSATION.stable_key,
        include_private=include_private,
        now=now,
    )


async def test_store_round_trips_fields_and_search_orders_by_similarity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(
        item(content="喜欢喝美式咖啡"), embedding=[1.0, 0.0, 0.0], embedding_model=MODEL
    )
    await repository.store(
        item(content="住在上海"), embedding=[0.0, 1.0, 0.0], embedding_model=MODEL
    )
    await repository.store(
        item(content="wrong-model row"), embedding=[1.0, 0.0, 0.0], embedding_model="other"
    )
    await repository.store(item(content="no-embedding row"), embedding=None, embedding_model=None)

    memories = await search(repository, query=[0.9, 0.1, 0.0])

    assert [memory.content for memory in memories] == ["喜欢喝美式咖啡", "住在上海"]
    assert memories[0].distance < memories[1].distance
    assert memories[0].scope is MemoryScope.SUBJECT
    assert memories[0].privacy is MemoryPrivacy.PRIVATE
    async with sessions() as session:
        touched = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM memory_items WHERE last_accessed_at IS NOT NULL"
                )
            )
        ).scalar_one()
    assert touched == 2


async def test_scope_filters_keep_other_subjects_and_conversations_out(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(
        item(content="mine"), embedding=[1.0, 0.0], embedding_model=MODEL
    )
    await repository.store(
        item(content="someone else's", subject=OTHER_SUBJECT),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(content="this conversation", scope=MemoryScope.CONVERSATION),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(
            content="another conversation",
            scope=MemoryScope.CONVERSATION,
            conversation=OTHER_CONVERSATION,
        ),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(content="global fact", scope=MemoryScope.GLOBAL, privacy=MemoryPrivacy.PUBLIC),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )

    contents = {memory.content for memory in await search(repository, query=[1.0, 0.0])}

    assert contents == {"mine", "this conversation", "global fact"}


async def test_private_and_sensitive_are_hidden_when_include_private_is_false(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(
        item(content="private fact"), embedding=[1.0, 0.0], embedding_model=MODEL
    )
    await repository.store(
        item(content="sensitive fact", privacy=MemoryPrivacy.SENSITIVE),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(
            content="shared context",
            scope=MemoryScope.CONVERSATION,
            privacy=MemoryPrivacy.SHARED,
        ),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )

    visible = {
        memory.content
        for memory in await search(repository, query=[1.0, 0.0], include_private=False)
    }

    assert visible == {"shared context"}


async def test_validity_window_and_revocation_filter_results(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(
        item(content="still valid", valid_until=NOW + timedelta(days=1)),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(content="already expired", valid_until=NOW - timedelta(days=1)),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(content="to be revoked"), embedding=[1.0, 0.0], embedding_model=MODEL
    )
    revoked = await repository.revoke_for(
        subject_identity_id=SUBJECT, conversation_stable_key=None, now=NOW
    )
    # revoke_for hits every SUBJECT item of the sender, so re-add one valid row
    await repository.store(
        item(content="fresh after forget"), embedding=[1.0, 0.0], embedding_model=MODEL
    )

    contents = [memory.content for memory in await search(repository, query=[1.0, 0.0])]

    assert revoked == 3
    assert contents == ["fresh after forget"]


async def test_supersedes_metadata_alone_does_not_bypass_temporal_invalidation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    old = item(content="旧偏好:喝茶")
    await repository.store(old, embedding=[1.0, 0.0], embedding_model=MODEL)
    await repository.store(
        item(content="新偏好:喝咖啡", supersedes=(old.id,)),
        embedding=[0.99, 0.01],
        embedding_model=MODEL,
    )

    contents = [memory.content for memory in await search(repository, query=[1.0, 0.0])]

    assert contents == ["新偏好:喝咖啡", "旧偏好:喝茶"]


async def test_lifecycle_expires_decays_and_purges_with_injected_clock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(
        item(content="expires now", valid_until=NOW - timedelta(hours=1)),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
    )
    stale = item(content="stale low confidence", confidence=0.22)
    await repository.store(stale, embedding=[1.0, 0.0], embedding_model=MODEL)
    async with sessions() as session:  # make it look untouched for 100 days
        await session.execute(
            sa.text("UPDATE memory_items SET created_at = :old WHERE id = :id"),
            {"old": NOW - timedelta(days=100), "id": stale.id},
        )
        await session.commit()
    long_revoked = item(content="long revoked")
    await repository.store(long_revoked, embedding=[1.0, 0.0], embedding_model=MODEL)
    async with sessions() as session:
        await session.execute(
            sa.text(
                "UPDATE memory_items SET revoked_at = :old, revoked_reason = 'user_forget' "
                "WHERE id = :id"
            ),
            {"old": NOW - timedelta(days=60), "id": long_revoked.id},
        )
        await session.commit()

    report = await repository.run_lifecycle(
        now=NOW,
        decay_days=90,
        decay_factor=0.8,
        confidence_floor=0.2,
        revoked_retention_days=30,
    )

    assert report.expired == 1
    assert report.decayed == 1  # 0.22 * 0.8 = 0.176 < 0.2
    assert report.deleted == 1
    async with sessions() as session:
        remaining = (
            await session.execute(sa.text("SELECT count(*) FROM memory_items"))
        ).scalar_one()
    assert remaining == 2  # expired + decayed rows remain (revoked), purged row gone


async def test_forget_in_direct_chat_also_revokes_conversation_memories(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    await repository.store(item(content="subject fact"), embedding=[1.0], embedding_model=MODEL)
    await repository.store(
        item(content="conversation fact", scope=MemoryScope.CONVERSATION),
        embedding=[1.0],
        embedding_model=MODEL,
    )
    await repository.store(
        item(
            content="other conversation fact",
            scope=MemoryScope.CONVERSATION,
            conversation=OTHER_CONVERSATION,
        ),
        embedding=[1.0],
        embedding_model=MODEL,
    )

    revoked = await repository.revoke_for(
        subject_identity_id=SUBJECT,
        conversation_stable_key=CONVERSATION.stable_key,
        now=NOW,
    )

    assert revoked == 2
    remaining = await search(
        repository, query=[1.0], conversation_key=OTHER_CONVERSATION.stable_key
    )
    assert [memory.content for memory in remaining] == ["other conversation fact"]


async def test_relationship_successors_and_expression_lookup_keep_m4_boundaries(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    first = MemoryItem(
        scope=MemoryScope.SUBJECT,
        subject_identity_id=SUBJECT,
        kind="RELATIONSHIP",
        content="初次认识, 交流礼貌",
        source_message_ids=("m1",),
        confidence=0.85,
        relationship_score=10.0,
        privacy=MemoryPrivacy.PRIVATE,
    )
    second = first.model_copy(
        update={"content": "逐渐熟悉, 喜欢直接沟通", "relationship_score": 25.0}
    )
    initial = await repository.upsert_relationship(first, source="test", now=NOW)
    successor = await repository.upsert_relationship(
        second, source="test", now=NOW + timedelta(minutes=1)
    )
    current = await repository.relationship_for(
        SUBJECT, now=NOW + timedelta(minutes=2)
    )

    assert initial.operation is MemoryOperation.ADD
    assert successor.operation is MemoryOperation.UPDATE
    assert successor.previous_memory_id == first.id
    assert current is not None
    assert current.content == "逐渐熟悉, 喜欢直接沟通"
    assert current.relationship_score == 25.0

    expression = MemoryItem(
        scope=MemoryScope.CONVERSATION,
        conversation=OTHER_CONVERSATION,
        kind="EXPRESSION",
        content="短句, 轻松收尾",
        source_message_ids=("m2",),
        confidence=0.9,
        privacy=MemoryPrivacy.SHARED,
    )
    await repository.store(expression, embedding=None, embedding_model=None)

    assert await repository.active_expressions(
        CONVERSATION.stable_key, now=NOW, limit=3
    ) == ()
    local = await repository.active_expressions(
        OTHER_CONVERSATION.stable_key, now=NOW, limit=3
    )
    assert [row.content for row in local] == ["短句, 轻松收尾"]


async def test_search_with_empty_table_returns_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)

    assert await search(repository, query=[1.0, 0.0]) == ()


async def test_hybrid_search_recalls_text_only_chinese_slang_and_audits_hash_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    text_only = item(content="群里把 YYDS 当作永远的神")
    await repository.store(text_only, embedding=None, embedding_model=None)

    recalled = await search(
        repository,
        query=[0.0, 1.0],
        query_text="YYDS 永远的神",
    )

    assert [memory.id for memory in recalled] == [text_only.id]
    assert recalled[0].vector_rank is None
    assert recalled[0].text_rank == 1
    async with sessions() as session:
        audit = (
            await session.execute(
                sa.text(
                    "SELECT query_hash, vector_ids, text_ids, selected_ids "
                    "FROM memory_recall_audit ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).mappings().one()
    assert audit["query_hash"] != "YYDS 永远的神"
    assert audit["vector_ids"] == []
    assert audit["text_ids"] == [str(text_only.id)]
    assert audit["selected_ids"] == [str(text_only.id)]


async def test_update_inserts_successor_and_invalidates_predecessor_atomically(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    old = item(content="喜欢加糖咖啡")
    await repository.store(old, embedding=[1.0, 0.0], embedding_model=MODEL)
    candidate_item = item(content="喜欢无糖美式")

    applied = await repository.apply_merge(
        candidate_item,
        MemoryMergeDecision(
            operation=MemoryOperation.UPDATE,
            target_id=old.id,
            content="喜欢无糖美式",
        ),
        embedding=[1.0, 0.0],
        embedding_model=MODEL,
        source="test",
        now=NOW,
    )

    assert applied.memory_id is not None
    assert applied.memory_id != old.id
    async with sessions() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT id, invalid_at, invalidated_by, supersedes "
                    "FROM memory_items ORDER BY created_at, id"
                )
            )
        ).mappings().all()
    predecessor = next(row for row in rows if row["id"] == old.id)
    successor = next(row for row in rows if row["id"] == applied.memory_id)
    assert predecessor["invalid_at"] == NOW
    assert predecessor["invalidated_by"] == applied.memory_id
    assert successor["supersedes"] == [str(old.id)]
    assert [memory.id for memory in await search(repository, query=[1.0, 0.0])] == [
        applied.memory_id
    ]


async def test_revoked_memory_cannot_be_selected_as_merge_target(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)
    old = item(content="已忘记的偏好")
    await repository.store(old, embedding=[1.0], embedding_model=MODEL)
    await repository.revoke_for(
        subject_identity_id=SUBJECT,
        conversation_stable_key=None,
        now=NOW,
    )

    applied = await repository.apply_merge(
        item(content="试图复活"),
        MemoryMergeDecision(
            operation=MemoryOperation.UPDATE,
            target_id=old.id,
            content="试图复活",
        ),
        embedding=[1.0],
        embedding_model=MODEL,
        source="test",
        now=NOW,
    )

    assert applied.operation is MemoryOperation.NOOP
    assert applied.applied is False
    async with sessions() as session:
        count = (
            await session.execute(sa.text("SELECT count(*) FROM memory_items"))
        ).scalar_one()
    assert count == 1
