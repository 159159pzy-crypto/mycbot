import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
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
    ImageSegment,
    MessageEnvelope,
    Platform,
    ReplyPlan,
    StickerSegment,
    TextSegment,
)
from mybot.repositories.conversations import ConversationRepository
from mybot.repositories.messages import MessageRepository

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="MYBOT_TEST_DATABASE_URL is not configured"),
]

ROOT = Path(__file__).resolve().parents[2]


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
            sa.text("TRUNCATE tool_invocations, turns, messages, conversations CASCADE")
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def conversation_key() -> ConversationKey:
    return ConversationKey(connection_id="qq-main", chat_kind=ChatKind.GROUP, chat_id="333")


def envelope(message_id: str = "901") -> MessageEnvelope:
    return MessageEnvelope.model_validate(
        {
            "id": f"qq:qq-main:{message_id}",
            "connection_id": "qq-main",
            "platform": Platform.QQ,
            "chat_kind": ChatKind.GROUP,
            "chat_id": "333",
            "sender_identity_id": "qq:10001",
            "occurred_at": "2026-07-26T04:00:00Z",
            "segments": [
                TextSegment(text="hello"),
                ImageSegment(url="https://img.example/x"),
            ],
            "raw_ref": {"message_type": "group"},
        }
    )


async def test_get_or_create_is_idempotent_and_race_safe(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = ConversationRepository(sessions)

    first, second = await asyncio.gather(
        repository.get_or_create(conversation_key(), platform=Platform.QQ),
        repository.get_or_create(conversation_key(), platform=Platform.QQ),
    )
    third = await repository.get_or_create(conversation_key(), platform=Platform.QQ)

    assert first.id == second.id == third.id
    assert first.stable_key == conversation_key().stable_key


async def test_by_stable_key_returns_delivery_details_or_none(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repository = ConversationRepository(sessions)
    created = await repository.get_or_create(conversation_key(), platform=Platform.QQ)

    detail = await repository.by_stable_key(conversation_key().stable_key)
    missing = await repository.by_stable_key("qq:qq-main:group:404")

    assert detail is not None
    assert detail.id == created.id
    assert detail.stable_key == created.stable_key
    assert detail.connection_id == "qq-main"
    assert detail.platform == Platform.QQ.value
    assert detail.chat_kind == ChatKind.GROUP.value
    assert detail.chat_id == "333"
    assert missing is None


async def test_record_inbound_round_trips_segments_and_reports_duplicates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)

    stored = await messages.record_inbound(conversation.id, envelope())
    duplicate = await messages.record_inbound(conversation.id, envelope())

    assert stored.duplicate is False
    assert duplicate.duplicate is True
    round_tripped = await messages.stored_segments(stored.id)
    assert round_tripped == [
        {"type": "text", "text": "hello"},
        {"type": "image", "url": "https://img.example/x", "alt_text": None},
    ]


async def test_outbound_lifecycle_backfills_platform_id_for_reply_detection(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)

    outbound_id = await messages.record_outbound(
        conversation.id,
        ReplyPlan(
            text_segments=("pong",),
            media_segments=(StickerSegment(id="14", name="smile"),),
        ),
    )
    assert await messages.stored_segments(outbound_id) == [
        {"type": "text", "text": "pong"},
        {"type": "sticker", "id": "14", "name": "smile"},
    ]
    assert await messages.recent_outbound_platform_ids(conversation.id) == ()

    await messages.mark_delivered(outbound_id, "5001")

    assert await messages.recent_outbound_platform_ids(conversation.id) == ("5001",)


async def test_record_inbound_touches_conversation_last_message_at(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)

    await messages.record_inbound(conversation.id, envelope())

    async with sessions() as session:
        last_message_at = (
            await session.execute(sa.text("SELECT last_message_at FROM conversations"))
        ).scalar_one()
    assert last_message_at is not None


async def test_turn_repository_round_trips_every_field(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from mybot.contracts import TurnAction, TurnDecision, TurnTrigger
    from mybot.repositories.turns import TurnRepository

    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)
    stored = await messages.record_inbound(conversation.id, envelope())
    decision = TurnDecision(
        action=TurnAction.AGENT,
        reason="mentioned",
        confidence=1.0,
        trigger=TurnTrigger.MENTION,
    )

    turn_id = await TurnRepository(sessions).record_turn(
        conversation.id,
        decision,
        outcome="replied",
        inbound_message_id=stored.id,
        model="deepseek-chat",
        prompt_tokens=120,
        completion_tokens=34,
        latency_ms=850,
    )

    async with sessions() as session:
        row = (
            await session.execute(sa.text("SELECT * FROM turns WHERE id = :id"), {"id": turn_id})
        ).mappings().one()
    assert row["action"] == "AGENT"
    assert row["trigger"] == "MENTION"
    assert row["outcome"] == "replied"
    assert row["model"] == "deepseek-chat"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 34
    assert row["latency_ms"] == 850
    assert row["error"] is None


async def test_recent_texts_returns_newest_first_text_extracts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)
    await messages.record_inbound(conversation.id, envelope("901"))
    await messages.record_outbound(conversation.id, ReplyPlan(text_segments=("回复一",)))

    recent = await messages.recent_texts(conversation.id, limit=10)

    assert [entry.direction for entry in recent] == ["outbound", "inbound"]
    assert recent[0].text == "回复一"
    assert recent[1].text == "hello\n[图片: 图片]"
    assert recent[1].sender == "qq:10001"


async def test_tool_invocation_repository_records_rows_linked_to_a_turn(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from mybot.contracts import TurnAction, TurnDecision, TurnTrigger
    from mybot.repositories.tool_invocations import (
        ToolInvocationRecord,
        ToolInvocationRepository,
    )
    from mybot.repositories.turns import TurnRepository

    conversations = ConversationRepository(sessions)
    conversation = await conversations.get_or_create(conversation_key(), platform=Platform.QQ)
    turn_id = await TurnRepository(sessions).record_turn(
        conversation.id,
        TurnDecision(
            action=TurnAction.AGENT,
            reason="mentioned",
            confidence=1.0,
            trigger=TurnTrigger.MENTION,
        ),
        outcome="replied",
    )

    repository = ToolInvocationRepository(sessions)
    await repository.record_many(
        turn_id,
        [
            ToolInvocationRecord(tool_id="web_search", ok=True, error_code=None, latency_ms=120),
            ToolInvocationRecord(
                tool_id="fetch_url", ok=False, error_code="ssrf_blocked", latency_ms=5
            ),
        ],
    )

    async with sessions() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT tool_id, ok, error_code FROM tool_invocations "
                    "WHERE turn_id = :turn ORDER BY tool_id"
                ),
                {"turn": turn_id},
            )
        ).mappings().all()
    assert [dict(row) for row in rows] == [
        {"tool_id": "fetch_url", "ok": False, "error_code": "ssrf_blocked"},
        {"tool_id": "web_search", "ok": True, "error_code": None},
    ]


async def test_system_kv_repository_gets_and_upserts_json_values(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from mybot.repositories.system_kv import SystemKvRepository

    async with sessions() as session:
        await session.execute(sa.text("DELETE FROM system_kv WHERE key LIKE 'agent.%'"))
        await session.commit()
    repository = SystemKvRepository(sessions)

    assert await repository.get("agent.system_prompt") is None
    await repository.set("agent.system_prompt", "你是一个友好的助手")
    await repository.set("agent.system_prompt", {"text": "updated", "version": 2})

    assert await repository.get("agent.system_prompt") == {"text": "updated", "version": 2}
