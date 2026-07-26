"""Load smoke: 100 messages through real Redis Streams and PostgreSQL."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mybot.adapters import InboundEvent
from mybot.contracts import ChatKind, MessageEnvelope, Platform, TextSegment
from mybot.engine.agent_turns import AgentTurnEngine
from mybot.repositories.conversations import ConversationRepository
from mybot.repositories.messages import MessageRepository
from mybot.repositories.system_kv import SystemKvRepository
from mybot.repositories.turns import TurnRepository
from mybot.runtime import ProcessMode
from mybot.services.agent_worker import DEFAULT_CAPABILITIES, AgentWorkerService

from mybot.infrastructure.streams import (  # isort: skip
    RedisStreamBackend,
    StreamConsumer,
    StreamPublisher,
    create_redis_backend,
)

DATABASE_URL = os.environ.get("MYBOT_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("MYBOT_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DATABASE_URL and REDIS_URL),
        reason="MYBOT_TEST_DATABASE_URL and MYBOT_TEST_REDIS_URL are not configured",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
INGEST = "mybot:load:ingest"
OUTBOUND = "mybot:load:outbound"
MESSAGE_COUNT = 100
DEADLINE_SECONDS = 30.0


class NeverReadiness:
    async def check(self):  # type: ignore[no-untyped-def]
        raise AssertionError("/ping must not probe readiness")


def ping(index: int) -> InboundEvent:
    return InboundEvent(
        envelope=MessageEnvelope(
            id=f"telegram:telegram-main:777:{10_000 + index}",
            connection_id="telegram-main",
            platform=Platform.TELEGRAM,
            chat_kind=ChatKind.DIRECT,
            chat_id=str(700 + (index % 10)),  # spread across 10 conversations
            sender_identity_id=f"telegram:{700 + (index % 10)}",
            occurred_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
            segments=(TextSegment(text="/ping"),),
        )
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


async def test_one_hundred_messages_flow_with_no_dead_letters(
    migrated_database_url: str,
) -> None:
    assert REDIS_URL is not None
    engine = create_async_engine(migrated_database_url)
    backend: RedisStreamBackend = create_redis_backend(REDIS_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "TRUNCATE tool_invocations, turns, memory_items, messages, "
                    "conversations CASCADE"
                )
            )
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        messages_repository = MessageRepository(sessions)
        service = AgentWorkerService(
            consumer=StreamConsumer(
                backend,
                stream=INGEST,
                group="load-workers",
                consumer="worker-load",
                dead_letter_stream=f"{INGEST}:dead",
                max_attempts=3,
                dedupe_ttl_seconds=60,
                dedupe_prefix="mybot:load:seen",
                block_ms=50,
                claim_min_idle_ms=5_000,
                read_count=32,
            ),
            outbound=StreamPublisher(backend=backend, stream=OUTBOUND, maxlen=1_000),
            conversations=ConversationRepository(sessions),
            messages=messages_repository,
            readiness=NeverReadiness(),
            agent=AgentTurnEngine(
                llm=None,
                persona=SystemKvRepository(sessions),
                history=messages_repository,
                turns=TurnRepository(sessions),
                budget=None,
                default_system_prompt="load",
            ),
            capabilities=DEFAULT_CAPABILITIES,
        )
        publisher = StreamPublisher(backend=backend, stream=INGEST, maxlen=1_000)
        for index in range(MESSAGE_COUNT):
            await publisher.publish(ping(index).model_dump_json())

        stop_event = asyncio.Event()
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(service.run(ProcessMode.AGENT_WORKER, stop_event))
        while asyncio.get_running_loop().time() - started < DEADLINE_SECONDS:
            if await backend.stream_len(OUTBOUND) >= MESSAGE_COUNT:
                break
            await asyncio.sleep(0.1)
        elapsed = asyncio.get_running_loop().time() - started
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

        assert await backend.stream_len(OUTBOUND) == MESSAGE_COUNT, (
            f"only {await backend.stream_len(OUTBOUND)} replies after {elapsed:.1f}s"
        )
        assert await backend.stream_len(f"{INGEST}:dead") == 0
        assert await backend.pending_count(INGEST, "load-workers") == 0
        async with engine.connect() as connection:
            outbound_rows = (
                await connection.execute(
                    sa.text("SELECT count(*) FROM messages WHERE direction = 'outbound'")
                )
            ).scalar_one()
        assert outbound_rows == MESSAGE_COUNT
        assert elapsed < DEADLINE_SECONDS
    finally:
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead")
        await backend.aclose()
        await engine.dispose()
