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

from mybot.contracts import ChatKind, ConversationKey, MemoryItem, MemoryPrivacy, MemoryScope
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
    include_private: bool = True,
    subject: str = SUBJECT,
    conversation_key: str | None = None,
    now: datetime = NOW,
):  # type: ignore[no-untyped-def]
    return await repository.search(
        query_embedding=query,
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


async def test_superseded_items_are_dropped_when_superseder_is_retrieved(
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

    assert contents == ["新偏好:喝咖啡"]


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


async def test_search_with_empty_table_returns_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = MemoryRepository(sessions)

    assert await search(repository, query=[1.0, 0.0]) == ()
