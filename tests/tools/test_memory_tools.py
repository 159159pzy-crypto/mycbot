from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    CoreBlock,
    CoreBlockLabel,
    ToolContext,
)
from mybot.repositories.core_memory import CoreBlockLimitError, CoreBlockRecord
from mybot.tools.memory import MemoryAppendTool, MemoryReplaceTool

NOW = datetime(2026, 7, 28, tzinfo=UTC)


@dataclass
class FakeWriter:
    fail: bool = False
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def append(self, **kwargs):  # type: ignore[no-untyped-def]
        return self._record("append", kwargs)

    async def replace(self, **kwargs):  # type: ignore[no-untyped-def]
        return self._record("replace", kwargs)

    def _record(self, operation: str, kwargs: dict[str, object]) -> CoreBlockRecord:
        if self.fail:
            raise CoreBlockLimitError("too large")
        self.calls.append((operation, kwargs))
        return CoreBlockRecord(
            block=CoreBlock(
                label=kwargs["label"],
                subject_identity_id=kwargs["subject_identity_id"],
                content=kwargs["content"],
                token_budget=kwargs["token_budget"],
                version=2,
            ),
            created_at=NOW,
            updated_at=NOW,
        )


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main",
            chat_kind=ChatKind.DIRECT,
            chat_id="777",
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=("memory.write",),
        correlation_id="message-1",
    )


@pytest.mark.asyncio
async def test_append_targets_current_user_profile_and_requires_policy_gate() -> None:
    writer = FakeWriter()
    tool = MemoryAppendTool(writer, 800, 600, approval_required=True)

    result = await tool.run(
        context(),
        {"label": "user_profile", "content": "用户偏好无糖美式"},
    )

    assert result.ok is True
    assert tool.spec.capabilities == ("memory.write",)
    assert tool.spec.approval_required is True
    assert writer.calls[0][0] == "append"
    assert writer.calls[0][1]["subject_identity_id"] == "telegram:777"
    assert writer.calls[0][1]["token_budget"] == 600


@pytest.mark.asyncio
async def test_replace_persona_is_global_and_budget_failure_is_structured() -> None:
    writer = FakeWriter(fail=True)
    tool = MemoryReplaceTool(writer, 800, 600)

    result = await tool.run(context(), {"label": "persona", "content": "简洁友好"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "memory_budget_exceeded"


@pytest.mark.asyncio
async def test_invalid_core_label_is_rejected_without_writing() -> None:
    writer = FakeWriter()
    tool = MemoryAppendTool(writer, 800, 600)

    result = await tool.run(context(), {"label": "other", "content": "x"})

    assert result.ok is False
    assert writer.calls == []
    assert CoreBlockLabel.PERSONA.value == "persona"
