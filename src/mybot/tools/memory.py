"""Policy-controlled self-edit tools for bounded core memory."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from mybot.contracts import (
    CoreBlock,
    CoreBlockLabel,
    ToolContext,
    ToolError,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from mybot.repositories.core_memory import CoreBlockLimitError, CoreBlockRecord


class CoreBlockWriter(Protocol):
    async def append(
        self,
        *,
        label: CoreBlockLabel,
        subject_identity_id: str | None,
        content: str,
        token_budget: int,
        source: str,
    ) -> CoreBlockRecord: ...

    async def replace(
        self,
        *,
        label: CoreBlockLabel,
        subject_identity_id: str | None,
        content: str,
        token_budget: int,
        source: str,
    ) -> CoreBlockRecord: ...


def _spec(tool_id: str, description: str, *, approval_required: bool) -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "id": tool_id,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "enum": ["persona", "user_profile"],
                    },
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["label", "content"],
                "additionalProperties": False,
            },
            "read_only": False,
            "idempotent": tool_id == "memory_replace",
            "risk": ToolRisk.MEDIUM,
            "capabilities": ["memory.write"],
            "approval_required": approval_required,
        }
    )


@dataclass(slots=True)
class MemoryAppendTool:
    repository: CoreBlockWriter
    persona_token_budget: int
    user_profile_token_budget: int
    approval_required: bool = True

    @property
    def spec(self) -> ToolSpec:
        return _spec(
            "memory_append",
            "Append a durable note to the persona or current user's profile core memory.",
            approval_required=self.approval_required,
        )

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        parsed = _arguments(
            context,
            arguments,
            persona_token_budget=self.persona_token_budget,
            user_profile_token_budget=self.user_profile_token_budget,
        )
        if isinstance(parsed, ToolResult):
            return parsed
        label, subject, content, budget = parsed
        try:
            record = await self.repository.append(
                label=label,
                subject_identity_id=subject,
                content=content,
                token_budget=budget,
                source="tool:memory_append",
            )
        except CoreBlockLimitError as error:
            return _failure("memory_budget_exceeded", str(error))
        return _success(record.block)


@dataclass(slots=True)
class MemoryReplaceTool:
    repository: CoreBlockWriter
    persona_token_budget: int
    user_profile_token_budget: int
    approval_required: bool = True

    @property
    def spec(self) -> ToolSpec:
        return _spec(
            "memory_replace",
            "Replace the persona or current user's profile core memory with new content.",
            approval_required=self.approval_required,
        )

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        parsed = _arguments(
            context,
            arguments,
            persona_token_budget=self.persona_token_budget,
            user_profile_token_budget=self.user_profile_token_budget,
        )
        if isinstance(parsed, ToolResult):
            return parsed
        label, subject, content, budget = parsed
        try:
            record = await self.repository.replace(
                label=label,
                subject_identity_id=subject,
                content=content,
                token_budget=budget,
                source="tool:memory_replace",
            )
        except CoreBlockLimitError as error:
            return _failure("memory_budget_exceeded", str(error))
        return _success(record.block)


def _arguments(
    context: ToolContext,
    arguments: Mapping[str, JsonValue],
    *,
    persona_token_budget: int,
    user_profile_token_budget: int,
) -> tuple[CoreBlockLabel, str | None, str, int] | ToolResult:
    raw_label = arguments.get("label")
    content = arguments.get("content")
    try:
        label = CoreBlockLabel(str(raw_label))
    except ValueError:
        return _failure("invalid_arguments", "label must be persona or user_profile")
    if not isinstance(content, str) or not content.strip():
        return _failure("invalid_arguments", "content must be a non-empty string")
    subject = context.actor_identity_id if label is CoreBlockLabel.USER_PROFILE else None
    budget = (
        user_profile_token_budget
        if label is CoreBlockLabel.USER_PROFILE
        else persona_token_budget
    )
    return label, subject, content.strip(), budget


def _success(block: CoreBlock) -> ToolResult:
    return ToolResult.success(
        {
            "id": str(block.id),
            "label": block.label.value,
            "subject_identity_id": block.subject_identity_id,
            "version": block.version,
            "token_budget": block.token_budget,
        }
    )


def _failure(code: str, message: str) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message))


__all__ = ["MemoryAppendTool", "MemoryReplaceTool"]
