import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mybot.api import create_app
from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    MessageEnvelope,
    Platform,
    ReplyPlan,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.infrastructure.health import ReadinessService
from mybot.infrastructure.model_routing import ModelCallAttempt, ModelPurpose
from mybot.repositories.conversations import ConversationRepository
from mybot.repositories.llm_calls import LlmCallLogRepository
from mybot.repositories.memory import MemoryRepository
from mybot.repositories.messages import MessageRepository
from mybot.repositories.tool_invocations import (
    ToolInvocationRecord,
    ToolInvocationRepository,
)
from mybot.repositories.traces import TraceSpanRepository
from mybot.repositories.turns import TurnRepository
from mybot.settings import Settings

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
TOKEN = "operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class AlwaysUpProbe:
    async def check(self) -> None:
        return None


@pytest.fixture(scope="module")
def migrated() -> str:
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
async def client(migrated: str) -> AsyncIterator[httpx.AsyncClient]:
    assert REDIS_URL is not None
    engine = create_async_engine(migrated)
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "TRUNCATE operator_audit, tool_invocations, turns, memory_items, "
                "messages, conversations, system_kv CASCADE"
            )
        )
    await engine.dispose()
    app = create_app(
        settings=Settings(
            operator_token=TOKEN,
            database_url=migrated,
            redis_url=REDIS_URL,
        ),
        readiness=ReadinessService(database=AlwaysUpProbe(), redis=AlwaysUpProbe()),
        bootstrap=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
        base_url="http://api",
    ) as instance:
        yield instance


async def _seed(migrated: str) -> tuple[str, str]:
    engine = create_async_engine(migrated)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    conversations = ConversationRepository(sessions)
    messages = MessageRepository(sessions)
    turns = TurnRepository(sessions)
    invocations = ToolInvocationRepository(sessions)
    memory = MemoryRepository(sessions)
    llm_calls = LlmCallLogRepository(sessions)
    traces = TraceSpanRepository(sessions)
    key = ConversationKey(
        connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
    )
    conversation = await conversations.get_or_create(key, platform=Platform.TELEGRAM)
    envelope = MessageEnvelope(
        id="telegram:telegram-main:777:1",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        chat_id="777",
        sender_identity_id="telegram:777",
        trace_id="trace-operator-1",
        occurred_at=datetime(2026, 7, 26, 6, tzinfo=UTC),
        segments=(TextSegment(text="讲个笑话"),),
    )
    stored = await messages.record_inbound(conversation.id, envelope)
    await traces.record(
        trace_id=envelope.trace_id,
        stage="llm.chat",
        duration_ms=900,
        message_id=stored.id,
        attributes={"model": "deepseek-chat", "prompt_tokens": 100},
    )
    await messages.record_outbound(conversation.id, ReplyPlan(text_segments=("好的",)))
    turn_id = await turns.record_turn(
        conversation.id,
        TurnDecision(
            action=TurnAction.AGENT,
            reason="dm",
            confidence=1.0,
            trigger=TurnTrigger.DIRECT_MESSAGE,
        ),
        outcome="replied",
        inbound_message_id=stored.id,
        model="deepseek-chat",
        prompt_tokens=100,
        completion_tokens=40,
        latency_ms=900,
    )
    await invocations.record_many(
        turn_id,
        [ToolInvocationRecord(tool_id="web_search", ok=True, error_code=None, latency_ms=120)],
    )
    await llm_calls.record(
        ModelCallAttempt(
            purpose=ModelPurpose.CHAT,
            channel="primary",
            model="deepseek-chat",
            status="success",
            prompt_tokens=100,
            completion_tokens=40,
            latency_ms=900,
            conversation_id=str(conversation.id),
            input_price_per_million=Decimal("0.50"),
            output_price_per_million=Decimal("1.50"),
            cost_usd_micros=110,
        )
    )
    memory_item = MemoryItem(
        scope=MemoryScope.SUBJECT,
        subject_identity_id="telegram:777",
        kind="preference",
        content="喜欢美式咖啡",
        source_message_ids=("telegram:telegram-main:777:1",),
        confidence=0.9,
        privacy=MemoryPrivacy.PRIVATE,
    )
    await memory.store(memory_item, embedding=[0.1, 0.2], embedding_model="test-embed")
    await engine.dispose()
    return str(conversation.id), str(memory_item.id)


@pytest.mark.asyncio
async def test_conversation_browsing_returns_turns_and_tool_detail(
    client: httpx.AsyncClient, migrated: str
) -> None:
    conversation_id, _ = await _seed(migrated)

    conversations = (await client.get("/operator/conversations", headers=AUTH)).json()
    assert conversations["conversations"][0]["id"] == conversation_id
    assert conversations["conversations"][0]["message_count"] == 2

    turns = (
        await client.get(
            f"/operator/conversations/{conversation_id}/turns", headers=AUTH
        )
    ).json()["turns"]
    assert turns[0]["outcome"] == "replied"
    assert turns[0]["tool_invocations"][0]["tool_id"] == "web_search"

    messages = (
        await client.get(
            f"/operator/conversations/{conversation_id}/messages", headers=AUTH
        )
    ).json()["messages"]
    assert [m["direction"] for m in messages] == ["inbound", "outbound"]
    traces = (
        await client.get(
            f"/operator/conversations/{conversation_id}/traces",
            params={"trace_id": "trace-operator-1"},
            headers=AUTH,
        )
    ).json()["traces"]
    assert traces[0]["trace_id"] == "trace-operator-1"
    assert traces[0]["stage"] == "llm.chat"
    assert traces[0]["duration_ms"] == 900
    assert traces[0]["attributes"]["prompt_tokens"] == 100
    assert messages[0]["text"] == "讲个笑话"


@pytest.mark.asyncio
async def test_memory_listing_and_revocation_with_audit(
    client: httpx.AsyncClient, migrated: str
) -> None:
    _, memory_id = await _seed(migrated)

    listed = (await client.get("/operator/memories", headers=AUTH)).json()["memories"]
    assert listed[0]["content"] == "喜欢美式咖啡"

    revoke = await client.post(f"/operator/memories/{memory_id}/revoke", headers=AUTH)
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] is True

    active = (await client.get("/operator/memories", headers=AUTH)).json()["memories"]
    assert active == []
    with_revoked = (
        await client.get(
            "/operator/memories", params={"include_revoked": True}, headers=AUTH
        )
    ).json()["memories"]
    assert with_revoked[0]["revoked_reason"] == "operator"

    audit = (await client.get("/operator/audit", headers=AUTH)).json()["entries"]
    assert audit[0]["action"] == "memory.revoke"


@pytest.mark.asyncio
async def test_persona_and_approvals_round_trip_with_audit(
    client: httpx.AsyncClient,
) -> None:
    default_persona = (await client.get("/operator/config/persona", headers=AUTH)).json()
    assert default_persona["override"] is None
    assert "MyBot" in default_persona["default"]

    await client.put(
        "/operator/config/persona",
        json={"system_prompt": "你是高冷的猫娘助手。"},
        headers=AUTH,
    )
    updated = (await client.get("/operator/config/persona", headers=AUTH)).json()
    assert updated["override"] == "你是高冷的猫娘助手。"

    await client.put(
        "/operator/config/approvals",
        json={"approved_ids": ["danger_tool", "  danger_tool ", ""]},
        headers=AUTH,
    )
    approvals = (await client.get("/operator/config/approvals", headers=AUTH)).json()
    assert approvals["approved_ids"] == ["danger_tool"]

    actions = {
        entry["action"]
        for entry in (await client.get("/operator/audit", headers=AUTH)).json()["entries"]
    }
    assert {"persona.update", "approvals.update"} <= actions


@pytest.mark.asyncio
async def test_usage_and_metrics_shapes(
    client: httpx.AsyncClient, migrated: str
) -> None:
    conversation_id, _ = await _seed(migrated)

    usage = (await client.get("/operator/usage", headers=AUTH)).json()["usage"]
    assert usage[-1]["tokens"] == 140

    metrics = (await client.get("/operator/metrics", headers=AUTH)).json()
    assert metrics["turns"]["by_outcome"]["replied"] == 1
    assert "ingest" in metrics["queues"]
    assert "outbound_dead_letter" in metrics["queues"]

    models = (await client.get("/operator/models", headers=AUTH)).json()
    assert models["usage"][0]["channel"] == "primary"
    assert models["usage"][0]["cost_usd_micros"] >= 110
    assert models["daily_usage"][-1]["calls"] >= 1
    assert models["conversation_usage"][0]["conversation_id"] == conversation_id


@pytest.mark.asyncio
async def test_proactive_optin_round_trip_with_audit(client: httpx.AsyncClient) -> None:
    initial = (await client.get("/operator/config/proactive", headers=AUTH)).json()
    assert initial["enabled_conversations"] == []
    assert initial["globally_enabled"] is False

    await client.put(
        "/operator/config/proactive",
        json={"enabled_conversations": ["v1:telegram-main:DIRECT:777:0", " ", "dup", "dup"]},
        headers=AUTH,
    )
    updated = (await client.get("/operator/config/proactive", headers=AUTH)).json()
    assert updated["enabled_conversations"] == ["dup", "v1:telegram-main:DIRECT:777:0"]

    actions = {
        entry["action"]
        for entry in (await client.get("/operator/audit", headers=AUTH)).json()["entries"]
    }
    assert "proactive.update" in actions


@pytest.mark.asyncio
async def test_revoking_missing_memory_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/operator/memories/{uuid4()}/revoke", headers=AUTH
    )
    assert response.status_code == 404
