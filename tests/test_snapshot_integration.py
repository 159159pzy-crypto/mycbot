import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mybot.snapshot import (
    DatabaseSnapshotRepository,
    SnapshotCoreBlock,
    SnapshotMemory,
    SnapshotProfile,
)

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="MYBOT_TEST_DATABASE_URL is not configured"),
]


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
        await connection.execute(
            sa.text(
                "TRUNCATE memory_operation_audit, core_blocks, memory_items, "
                "persona_versions, agent_profiles CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def memory(*, content: str, memory_id: UUID | None = None) -> SnapshotMemory:
    return SnapshotMemory(
        id=memory_id or uuid4(),
        scope="GLOBAL",
        kind="FACT",
        content=content,
        source_message_ids=("snapshot:test:1",),
        confidence=0.9,
        privacy="PRIVATE",
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


async def test_database_snapshot_merge_is_idempotent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = DatabaseSnapshotRepository(sessions)
    profiles = (SnapshotProfile(name="snapshot-test", system_prompt="你是测试助手。"),)
    blocks = (SnapshotCoreBlock(label="persona", content="保持简洁。", token_budget=400),)
    memories = (memory(content="用户偏好简洁回答。"),)

    first = (
        await repository.merge_profiles(profiles),
        await repository.merge_core_blocks(blocks),
        await repository.merge_memories(memories),
    )
    second = (
        await repository.merge_profiles(profiles),
        await repository.merge_core_blocks(blocks),
        await repository.merge_memories(memories),
    )

    assert first == (1, 1, 1)
    assert second == (0, 0, 0)
    async with sessions() as session:
        assert await session.scalar(sa.text("SELECT count(*) FROM agent_profiles")) == 1
        assert await session.scalar(sa.text("SELECT count(*) FROM persona_versions")) == 1
        assert await session.scalar(sa.text("SELECT count(*) FROM core_blocks")) == 1
        assert await session.scalar(sa.text("SELECT version FROM core_blocks")) == 1
        assert await session.scalar(sa.text("SELECT count(*) FROM memory_items")) == 1


async def test_memory_uuid_conflict_is_reported_as_noop(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = DatabaseSnapshotRepository(sessions)
    memory_id = uuid4()

    assert await repository.merge_memories((memory(content="原始内容", memory_id=memory_id),)) == 1
    assert await repository.merge_memories((memory(content="冲突内容", memory_id=memory_id),)) == 0

    async with sessions() as session:
        rows = (
            await session.execute(sa.text("SELECT id, content FROM memory_items"))
        ).all()
    assert rows == [(memory_id, "原始内容")]
