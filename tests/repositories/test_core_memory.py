import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mybot.contracts import (
    CoreBlockLabel,
    MemoryItem,
    MemoryMergeDecision,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
)
from mybot.repositories.core_memory import CoreBlockLimitError, CoreBlockRepository
from mybot.repositories.memory import MemoryRepository
from mybot.repositories.operator_views import OperatorViews

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 28, tzinfo=UTC)

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
            sa.text("TRUNCATE core_blocks, memory_items, memory_operation_audit")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_core_blocks_are_versioned_bounded_and_owner_scoped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = CoreBlockRepository(sessions)
    created = await repository.replace(
        label=CoreBlockLabel.USER_PROFILE,
        subject_identity_id="telegram:777",
        content="偏好无糖美式",
        token_budget=50,
        source="test",
    )
    updated = await repository.append(
        label=CoreBlockLabel.USER_PROFILE,
        subject_identity_id="telegram:777",
        content="在上海工作",
        token_budget=999,
        source="test",
    )

    assert created.block.version == 1
    assert updated.block.version == 2
    assert updated.block.token_budget == 50
    visible = await repository.visible_for("telegram:777")
    assert [record.block.content for record in visible] == ["偏好无糖美式\n在上海工作"]
    assert await repository.visible_for("telegram:999") == ()

    with pytest.raises(CoreBlockLimitError):
        await repository.append(
            label=CoreBlockLabel.USER_PROFILE,
            subject_identity_id="telegram:777",
            content="超长" * 200,
            token_budget=50,
            source="test",
        )


async def test_operator_views_expose_temporal_history_audit_and_core_blocks(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    memories = MemoryRepository(sessions)
    old = MemoryItem(
        scope=MemoryScope.SUBJECT,
        subject_identity_id="telegram:777",
        kind="preference",
        content="喜欢加糖咖啡",
        source_message_ids=("message-1",),
        confidence=0.8,
        privacy=MemoryPrivacy.PRIVATE,
    )
    await memories.store(old, embedding=[1.0], embedding_model="test")
    applied = await memories.apply_merge(
        MemoryItem(
            scope=MemoryScope.SUBJECT,
            subject_identity_id="telegram:777",
            kind="preference",
            content="喜欢无糖美式",
            source_message_ids=("message-2",),
            confidence=0.9,
            privacy=MemoryPrivacy.PRIVATE,
        ),
        MemoryMergeDecision(
            operation=MemoryOperation.UPDATE,
            target_id=old.id,
            content="喜欢无糖美式",
        ),
        embedding=[1.0],
        embedding_model="test",
        source="test",
        now=NOW,
    )
    assert applied.memory_id is not None
    views = OperatorViews(sessions)

    listed = await views.memories(state="all")
    assert {str(item["state"]) for item in listed} == {"active", "invalidated"}
    history = await views.memory_history(applied.memory_id)
    assert [item["content"] for item in history] == ["喜欢加糖咖啡", "喜欢无糖美式"]
    operations = await views.memory_operations(applied.memory_id)
    assert operations[0]["operation"] == "UPDATE"

    saved = await views.replace_core_block(
        label="persona",
        subject_identity_id=None,
        content="简洁友好",
        token_budget=100,
    )
    assert saved["version"] == 1
    assert (await views.core_blocks())[0]["content"] == "简洁友好"
