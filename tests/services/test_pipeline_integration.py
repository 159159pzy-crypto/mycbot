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
from mybot.repositories.traces import TraceSpanRepository
from mybot.repositories.turns import TurnRepository
from mybot.runtime import ProcessMode
from mybot.services.agent_worker import DEFAULT_CAPABILITIES, AgentWorkerService
from mybot.services.gateway import GatewayService, SandboxSender

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
DEDUPE_KEY = "mybot:e2e:seen:telegram:telegram-main:777:1001"
SANDBOX_INGEST = "mybot:e2e:sandbox:ingest"
SANDBOX_OUTBOUND = "mybot:e2e:sandbox:outbound"
SANDBOX_DEDUPE_KEY = "mybot:e2e:sandbox:seen:sandbox:sandbox:debug-1:1001"


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
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead", DEDUPE_KEY)
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
        await backend.delete(INGEST, OUTBOUND, f"{INGEST}:dead", DEDUPE_KEY)
        await backend.aclose()
        await engine.dispose()


async def test_sandbox_uses_real_streams_persistence_gateway_and_trace_path(
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
        await backend.delete(
            SANDBOX_INGEST,
            SANDBOX_OUTBOUND,
            f"{SANDBOX_INGEST}:dead",
            f"{SANDBOX_OUTBOUND}:dead",
            SANDBOX_DEDUPE_KEY,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        messages = MessageRepository(sessions)
        traces = TraceSpanRepository(sessions)
        agent = AgentTurnEngine(
            llm=None,
            persona=SystemKvRepository(sessions),
            history=messages,
            turns=TurnRepository(sessions),
            budget=None,
            default_system_prompt="test persona",
        )
        worker = AgentWorkerService(
            consumer=StreamConsumer(
                backend,
                stream=SANDBOX_INGEST,
                group="sandbox-workers",
                consumer="sandbox-worker-e2e",
                dead_letter_stream=f"{SANDBOX_INGEST}:dead",
                max_attempts=3,
                dedupe_ttl_seconds=30,
                dedupe_prefix="mybot:e2e:sandbox:seen",
                block_ms=50,
                claim_min_idle_ms=1_000,
            ),
            outbound=StreamPublisher(backend=backend, stream=SANDBOX_OUTBOUND, maxlen=100),
            conversations=ConversationRepository(sessions),
            messages=messages,
            readiness=HealthyReadiness(),
            agent=agent,
            capabilities=DEFAULT_CAPABILITIES,
            traces=traces,
        )
        gateway = GatewayService(
            qq=None,
            telegram=None,
            sandbox=SandboxSender(),
            outbound_consumer=StreamConsumer(
                backend,
                stream=SANDBOX_OUTBOUND,
                group="sandbox-gateway",
                consumer="sandbox-gateway-e2e",
                dead_letter_stream=f"{SANDBOX_OUTBOUND}:dead",
                max_attempts=3,
                dedupe_ttl_seconds=30,
                dedupe_prefix="mybot:e2e:sandbox:outbound-seen",
                block_ms=50,
                claim_min_idle_ms=1_000,
            ),
            deliveries=messages,
            traces=traces,
        )
        stop_event = asyncio.Event()
        worker_task = asyncio.create_task(worker.run(ProcessMode.AGENT_WORKER, stop_event))
        gateway_task = asyncio.create_task(gateway.run(ProcessMode.GATEWAY, stop_event))
        envelope = MessageEnvelope(
            id="sandbox:sandbox:debug-1:1001",
            connection_id="sandbox",
            platform=Platform.SANDBOX,
            chat_kind=ChatKind.DIRECT,
            chat_id="debug-1",
            sender_identity_id="sandbox:operator",
            occurred_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
            segments=(TextSegment(text="/ping"),),
            trace_id="trace-sandbox-e2e",
            ephemeral=True,
        )
        await StreamPublisher(
            backend=backend, stream=SANDBOX_INGEST, maxlen=100
        ).publish(InboundEvent(envelope=envelope).model_dump_json())

        deadline = asyncio.get_running_loop().time() + 10.0
        delivered = False
        while asyncio.get_running_loop().time() < deadline:
            async with engine.connect() as connection:
                delivered = bool(
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT count(*) FROM messages "
                                "WHERE direction = 'outbound' AND platform_message_id IS NOT NULL"
                            )
                        )
                    ).scalar_one()
                )
            if delivered:
                break
            await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(asyncio.gather(worker_task, gateway_task), timeout=5.0)

        assert delivered is True
        async with engine.connect() as connection:
            conversation = (
                await connection.execute(
                    sa.text("SELECT ephemeral FROM conversations WHERE chat_id = 'debug-1'")
                )
            ).mappings().one()
            outbound = (
                await connection.execute(
                    sa.text(
                        "SELECT trace_id, platform_message_id FROM messages "
                        "WHERE direction = 'outbound'"
                    )
                )
            ).mappings().one()
            stages = (
                await connection.execute(
                    sa.text(
                        "SELECT stage FROM trace_spans "
                        "WHERE trace_id = 'trace-sandbox-e2e' ORDER BY created_at, stage"
                    )
                )
            ).scalars().all()
        assert conversation["ephemeral"] is True
        assert outbound["trace_id"] == "trace-sandbox-e2e"
        assert str(outbound["platform_message_id"]).startswith("sandbox:")
        assert set(stages) == {
            "ingest.persist",
            "moderation.outbound",
            "outbound.delivery",
            "outbound.publish",
            "turn.decision",
        }
    finally:
        await backend.delete(
            SANDBOX_INGEST,
            SANDBOX_OUTBOUND,
            f"{SANDBOX_INGEST}:dead",
            f"{SANDBOX_OUTBOUND}:dead",
            SANDBOX_DEDUPE_KEY,
        )
        await backend.aclose()
        await engine.dispose()
