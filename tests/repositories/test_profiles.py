import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mybot.contracts import (
    AgentProfile,
    ChatKind,
    ConversationKey,
    Platform,
    ProfileMemoryPolicy,
    ReplyWillingnessPolicy,
)
from mybot.repositories.conversations import ConversationRepository
from mybot.repositories.profiles import ProfileRepository
from mybot.repositories.system_kv import SystemKvRepository

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[2]

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
                "TRUNCATE conversation_profile_bindings, persona_versions, "
                "agent_profiles, conversations, system_kv CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_default_profile_is_idempotent_and_resolves_for_unbound_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    conversation = await conversations.get_or_create(
        ConversationKey(connection_id="tg", chat_kind=ChatKind.DIRECT, chat_id="7"),
        platform=Platform.TELEGRAM,
    )
    profiles = ProfileRepository(sessions)

    first = await profiles.ensure_default(
        system_prompt="你是 MyBot。",
        tool_capabilities=("web.search",),
    )
    second = await profiles.ensure_default(
        system_prompt="ignored after initialization",
        tool_capabilities=("web.fetch",),
    )
    resolved = await profiles.resolve(
        conversation.id,
        default_system_prompt="fallback",
        default_tool_capabilities=(),
    )

    assert second.profile.id == first.profile.id
    assert resolved.profile.id == first.profile.id
    assert resolved.persona.system_prompt == "你是 MyBot。"
    assert resolved.profile.tool_capabilities == ("web.search",)


async def test_profile_binding_policy_update_and_persona_rollback_create_new_versions(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    conversation = await conversations.get_or_create(
        ConversationKey(connection_id="qq", chat_kind=ChatKind.GROUP, chat_id="42"),
        platform=Platform.QQ,
    )
    profiles = ProfileRepository(sessions)
    created = await profiles.create(
        AgentProfile(
            name="闲聊群",
            description="活泼但克制",
            model_tier="economy",
            tool_capabilities=("web.search",),
            memory=ProfileMemoryPolicy(expression_examples=4),
            willingness=ReplyWillingnessPolicy(
                enabled=True,
                threshold=0.74,
                keywords=("麦麦",),
            ),
        ),
        system_prompt="第一版人设",
        change_note="initial",
    )
    await profiles.bind(conversation.id, created.profile.id)
    second = await profiles.create_persona_version(
        created.profile.id,
        system_prompt="第二版人设",
        change_note="warmer",
    )
    rollback = await profiles.rollback_persona(
        created.profile.id,
        target_version_id=created.persona.id,
    )
    resolved = await profiles.resolve(
        conversation.id,
        default_system_prompt="fallback",
        default_tool_capabilities=(),
    )
    versions = await profiles.persona_versions(created.profile.id)

    assert second.version == 2
    assert rollback.version == 3
    assert rollback.parent_version_id == created.persona.id
    assert rollback.system_prompt == "第一版人设"
    assert resolved.profile.name == "闲聊群"
    assert resolved.profile.model_tier == "economy"
    assert resolved.profile.willingness.enabled is True
    assert [version.version for version in versions] == [3, 2, 1]


async def test_default_profile_seeds_from_the_existing_runtime_persona_override(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    config = SystemKvRepository(sessions)
    await config.set("agent.system_prompt", "迁移前的运行时人设")
    profiles = ProfileRepository(sessions, legacy_persona=config)

    resolved = await profiles.ensure_default(
        system_prompt="环境默认人设",
        tool_capabilities=(),
    )

    assert resolved.persona.system_prompt == "迁移前的运行时人设"
