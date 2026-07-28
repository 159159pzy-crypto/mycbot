import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from mybot.tools import ToolExecutor, ToolRegistry


@dataclass
class DangerousTool:
    calls: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            id="danger",
            description="danger",
            input_schema={"type": "object"},
            read_only=False,
            idempotent=False,
            risk=ToolRisk.HIGH,
            approval_required=True,
        )

    async def run(self, context, arguments):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ToolResult.success({"done": True})


class Standing:
    async def is_approved(self, _tool_id: str) -> bool:
        return False


@dataclass
class PerCall:
    status: str
    calls: int = 0

    async def request_approval(self, tool_id, context, arguments):  # type: ignore[no-untyped-def]
        self.calls += 1
        await asyncio.sleep(0)
        return self.status


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="qq-main", chat_kind=ChatKind.DIRECT, chat_id="1"
        ),
        actor_identity_id="qq:1",
        correlation_id="message-1",
    )


@pytest.mark.asyncio
async def test_per_call_approval_executes_only_after_approval() -> None:
    tool = DangerousTool()
    executor = ToolExecutor(
        ToolRegistry([tool]), approvals=Standing(), approval_requests=PerCall("APPROVED")
    )

    result = await executor.execute("danger", context(), {})

    assert result.ok is True
    assert tool.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"), [("REJECTED", "approval_rejected"), ("EXPIRED", "approval_timeout")]
)
async def test_rejected_or_expired_approval_never_executes(status: str, code: str) -> None:
    tool = DangerousTool()
    executor = ToolExecutor(
        ToolRegistry([tool]), approvals=Standing(), approval_requests=PerCall(status)
    )

    result = await executor.execute("danger", context(), {})

    assert result.ok is False
    assert result.error is not None and result.error.code == code
    assert tool.calls == 0
