"""End-to-end spine smoke: real Redis Streams and PostgreSQL, fake platforms."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.contracts import ChatKind, MessageEnvelope, Platform, TextSegment
from mybot.engine.agent_turns import AgentTurnEngine
from mybot.infrastructure.streams import (
    RedisStreamBackend,
    StreamConsumer,
    StreamPublisher,
    create_redis_backend,
)
from mybot.repositories.conversations import ConversationRepository
from mybot.repositories.messages import MessageRepository
from mybot.repositories.system_kv import SystemKvRepository
from mybot.repositories.turns import TurnRepository
from mybot.runtime import ProcessMode
from mybot.services.agent_worker import DEFAULT_CAPABILITIES, AgentWorkerService

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
INGEST = "mybot:e2e:ingest"
OUTBOUND = "mybot:e2e:outbound"


class HealthyReadiness:
    async def check(self):  # type: ignore[no-untyped-def]
        raise AssertionError("the /ping path must not probe readiness")


def ping_envelope() -> MessageEnvelope:
    return MessageEnvelope(
        id="telegram:telegram-main:777:1001",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        chat_id="777",
        sender_identity_id="telegram:777",
        occurred_at=datetime(2026, 7, 26, 5, tzinfo=UTC),
        segments=(TextSegment(text="/ping"),),
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


async def test_ping_flows_from_ingest_stream_to_outbound_stream_and_database(
    migrated_database_url: str,
) -> None:
    assert REDIS_URL is not None
    engine = create_async_engine(migrated_database_url)
    backend: RedisStreamBackend = create_redis_backend(REDIS_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(
            sa.text("TRUNCATE tool_invocations, turns, messages, conversations CASCADE")
        )
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        messages_repository = MessageRepository(sessions)
        agent = AgentTurnEngine(
            llm=None,  # the /ping path never reaches the agent
            persona=SystemKvRepository(sessions),
            history=messages_repository,
            turns=TurnRepository(sessions),
            budget=None,
            default_system_prompt="test persona",
        )
        service = AgentWorkerService(
            consumer=StreamConsumer(
                backend,
                stream=INGEST,
                group="e2e-workers",
                consumer="worker-e2e",
                dead_letter_stream=f"{INGEST}:dead",
                max_attempts=3,
                dedupe_ttl_seconds=30,
                dedupe_prefix="mybot:e2e:seen",
                block_ms=50,
                claim_min_idle_ms=1_000,
            ),
            outbound=StreamPublisher(backend=backend, stream=OUTBOUND, maxlen=100),
            conversations=ConversationRepository(sessions),
            messages=messages_repository,
            readiness=HealthyReadiness(),
            agent=agent,
            capabilities=DEFAULT_CAPABILITIES,
        )
        ingest_publisher = StreamPublisher(backend=backend, stream=INGEST, maxlen=100)
        stop_event = asyncio.Event()
        task = asyncio.create_task(service.run(ProcessMode.AGENT_WORKER, stop_event))

        await backend.ensure_group(OUTBOUND, "e2e-observer")
        event = InboundEvent(envelope=ping_envelope())
        await ingest_publisher.publish(event.model_dump_json())
        await ingest_publisher.publish(event.model_dump_json())  # dedupe candidate

        deadline = asyncio.get_running_loop().time() + 10.0
        outbound_payloads: list[str] = []
        while asyncio.get_running_loop().time() < deadline:
            response = await backend.read_new(
                OUTBOUND, "e2e-observer", "observer", count=10, block_ms=100
            )
            outbound_payloads.extend(payload for _, payload in response)
            if outbound_payloads:
                break
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

        assert len(outbound_payloads) == 1, "dedupe must keep exactly one reply"
        reply = OutboundMessage.model_validate_json(outbound_payloads[0])
        assert reply.reply_plan.text_segments == ("pong",)
        assert reply.platform is Platform.TELEGRAM

        async with engine.connect() as connection:
            inbound_count = (
                await connection.execute(
                    sa.text("SELECT count(*) FROM messages WHERE direction = 'inbound'")
                )
            ).scalar_one()
            outbound_count = (
                await connection.execute(
                    sa.text("SELECT count(*) FROM messages WHERE direction = 'outbound'")
                )
            ).scalar_one()
        assert inbound_count == 1
        assert outbound_count == 1
    finally:
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead")
        await backend.aclose()
        await engine.dispose()
