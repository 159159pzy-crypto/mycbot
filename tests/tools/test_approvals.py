from dataclasses import dataclass
from uuid import uuid4

import pytest
from pydantic import JsonValue

from mybot.contracts import ChatKind, ConversationKey, ToolContext, ToolResult, ToolRisk, ToolSpec
from mybot.tools import ToolExecutor, ToolRegistry
from mybot.tools.approvals import APPROVALS_KEY, SystemKvApprovals


def spec(tool_id: str, *, approval_required: bool) -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "id": tool_id,
            "description": "x",
            "input_schema": {"type": "object", "properties": {}},
            "read_only": True,
            "idempotent": True,
            "risk": ToolRisk.MEDIUM,
            "capabilities": [],
            "approval_required": approval_required,
        }
    )


@dataclass
class RanTool:
    spec_value: ToolSpec

    @property
    def spec(self) -> ToolSpec:
        return self.spec_value

    async def run(self, context, arguments):  # type: ignore[no-untyped-def]
        return ToolResult.success({"ran": True})


@dataclass
class FakeConfig:
    approved: list[str]
    reads: int = 0

    async def get(self, key: str) -> JsonValue | None:
        assert key == APPROVALS_KEY
        self.reads += 1
        return list(self.approved)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=(),
        correlation_id="corr-1",
    )


@pytest.mark.asyncio
async def test_approval_required_tool_refused_without_approval_source() -> None:
    registry = ToolRegistry([RanTool(spec("danger", approval_required=True))])
    executor = ToolExecutor(registry)

    result = await executor.execute("danger", context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "approval_required"


@pytest.mark.asyncio
async def test_approved_tool_runs_and_unapproved_stays_blocked() -> None:
    registry = ToolRegistry([RanTool(spec("danger", approval_required=True))])
    approvals = SystemKvApprovals(config=FakeConfig(approved=["danger"]))
    executor = ToolExecutor(registry, approvals=approvals)

    assert (await executor.execute("danger", context(), {})).ok is True

    registry2 = ToolRegistry([RanTool(spec("other", approval_required=True))])
    blocked = ToolExecutor(
        registry2, approvals=SystemKvApprovals(config=FakeConfig(approved=["danger"]))
    )
    assert (await blocked.execute("other", context(), {})).ok is False


@pytest.mark.asyncio
async def test_approvals_are_ttl_cached() -> None:
    clock = Clock()
    config = FakeConfig(approved=["danger"])
    approvals = SystemKvApprovals(config=config, ttl_seconds=10.0, clock=clock)

    assert await approvals.is_approved("danger") is True
    assert await approvals.is_approved("danger") is True
    assert config.reads == 1  # cached within TTL

    clock.now += 11.0
    assert await approvals.is_approved("danger") is True
    assert config.reads == 2
