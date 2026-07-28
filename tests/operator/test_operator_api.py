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
    ModerationDecision,
    ModerationPoint,
    ModerationRequest,
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
from mybot.repositories.safety import ModerationAuditRepository, ToolApprovalRepository
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
                "TRUNCATE moderation_audit, tool_approval_request, evaluation_result, "
                "evaluation_run, message_feedback, operator_audit, tool_invocations, "
                "turns, memory_items, "
                "agent_profiles, messages, conversations, system_kv CASCADE"
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
async def test_m7_policy_per_call_approval_and_feedback(
    client: httpx.AsyncClient, migrated: str
) -> None:
    saved_policy = (
        await client.put(
            "/operator/safety/moderation-policy",
            headers=AUTH,
            json={
                "enabled": True,
                "backends": ["local"],
                "fail_mode": "closed",
                "keywords": ["blocked-term"],
                "inbound": {
                    "enabled": True,
                    "action": "direct_output",
                    "preset_response": "blocked inbound",
                },
                "outbound": {
                    "enabled": True,
                    "action": "overridden",
                    "preset_response": "safe outbound",
                },
            },
        )
    ).json()["policy"]
    assert saved_policy["fail_mode"] == "closed"
    assert saved_policy["outbound"]["action"] == "overridden"
    loaded_policy = (
        await client.get("/operator/safety/moderation-policy", headers=AUTH)
    ).json()["policy"]
    assert loaded_policy == saved_policy

    engine = create_async_engine(migrated)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await ModerationAuditRepository(sessions).record(
        ModerationRequest(point=ModerationPoint.INBOUND, params={"text": "hello"}),
        ModerationDecision(backend="local", reason="clear"),
        content_sha256="0" * 64,
        content_preview="hello",
        duration_ms=1,
    )
    moderation_entries = (
        await client.get("/operator/safety/moderation-audit", headers=AUTH)
    ).json()["entries"]
    assert moderation_entries[0]["backend"] == "local"
    assert isinstance(moderation_entries[0]["id"], str)
    approvals = ToolApprovalRepository(sessions)
    approval_id = uuid4()
    await approvals.request(
        invocation_id=approval_id,
        tool_id="memory.append",
        conversation_stable_key="v1:telegram-main:DIRECT:777:0",
        actor_identity_id="telegram:777",
        correlation_id="message-1",
        arguments={"content": "likes coffee"},
        timeout_seconds=30,
    )
    pending = (
        await client.get("/operator/safety/approvals", headers=AUTH)
    ).json()["requests"]
    assert pending[0]["id"] == str(approval_id)
    decision = await client.post(
        f"/operator/safety/approvals/{approval_id}",
        headers=AUTH,
        json={"approved": True, "note": "one call"},
    )
    assert decision.status_code == 200
    assert (await approvals.status(approval_id)).status == "APPROVED"

    conversation_id, _ = await _seed(migrated)
    messages = (
        await client.get(
            f"/operator/conversations/{conversation_id}/messages", headers=AUTH
        )
    ).json()["messages"]
    outbound_id = next(
        item["id"] for item in messages if item["direction"] == "outbound"
    )
    feedback = await client.put(
        f"/operator/messages/{outbound_id}/feedback",
        headers=AUTH,
        json={"rating": "NEGATIVE", "note": "too verbose"},
    )
    assert feedback.status_code == 200
    refreshed = (
        await client.get(
            f"/operator/conversations/{conversation_id}/messages", headers=AUTH
        )
    ).json()["messages"]
    outbound = next(item for item in refreshed if item["direction"] == "outbound")
    assert outbound["feedback"] == {"rating": "NEGATIVE", "note": "too verbose"}
    metrics = (await client.get("/operator/metrics", headers=AUTH)).json()["feedback"]
    assert metrics["negative_rate"] == 1.0

    cases = (await client.get("/operator/evaluations/cases", headers=AUTH)).json()["cases"]
    assert cases
    run_response = await client.post(
        "/operator/evaluations/runs",
        headers=AUTH,
        json={
            "name": "M7 smoke",
            "case_ids": [cases[0]["id"]],
            "judge_enabled": True,
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]
    detail = (
        await client.get(f"/operator/evaluations/runs/{run_id}", headers=AUTH)
    ).json()
    assert detail["run"]["judge_enabled"] is False
    assert detail["results"][0]["status"] == "RUNNING"
    assert detail["results"][0]["conversation_id"] is not None
    await engine.dispose()


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


@pytest.mark.asyncio
async def test_profile_persona_binding_and_relationship_management(
    client: httpx.AsyncClient, migrated: str
) -> None:
    conversation_id, _ = await _seed(migrated)
    created_response = await client.post(
        "/operator/profiles",
        headers=AUTH,
        json={
            "name": "闲聊群",
            "description": "轻松但不刷屏",
            "system_prompt": "你是群里克制而友好的伙伴",
            "model_tier": "economy",
            "tool_capabilities": ["web.search"],
            "memory": {
                "enabled": True,
                "retrieval_limit": 5,
                "expression_examples": 3,
                "relationship_enabled": True,
            },
            "willingness": {
                "enabled": True,
                "threshold": 0.8,
                "sensitivity": 0.9,
                "keywords": ["MyBot"],
            },
        },
    )
    assert created_response.status_code == 200
    profile_id = created_response.json()["profile"]["profile"]["id"]

    bound = await client.put(
        f"/operator/conversations/{conversation_id}/profile",
        headers=AUTH,
        json={"profile_id": profile_id},
    )
    assert bound.status_code == 200
    version = await client.post(
        f"/operator/profiles/{profile_id}/personas",
        headers=AUTH,
        json={"system_prompt": "新版群聊人设", "change_note": "更克制"},
    )
    assert version.status_code == 200
    target_id = version.json()["version"]["id"]
    rollback = await client.post(
        f"/operator/profiles/{profile_id}/personas/rollback",
        headers=AUTH,
        json={"target_version_id": target_id},
    )
    assert rollback.json()["version"]["version"] == 3

    profiles = (await client.get("/operator/profiles", headers=AUTH)).json()
    assert any(row["profile"]["id"] == profile_id for row in profiles["profiles"])
    assert profiles["bindings"] == [
        {"conversation_id": conversation_id, "profile_id": profile_id}
    ]

    relationship = await client.put(
        "/operator/relationships/telegram:777",
        headers=AUTH,
        json={"impression": "认真准备重要事情", "familiarity": 42.5},
    )
    assert relationship.status_code == 200
    listed = (await client.get("/operator/relationships", headers=AUTH)).json()
    assert listed["relationships"][0]["familiarity"] == 42.5
    assert listed["relationships"][0]["subject_identity_id"] == "telegram:777"
