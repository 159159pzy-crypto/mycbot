"""Policy-aware tool declaration, execution context, and result contracts."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.conversation import ConversationKey
from mybot.contracts.json import (
    FrozenJsonObjectValue,
    empty_frozen_json_object,
)


class ToolRisk(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolSpec(FrozenModel):
    id: NonEmptyStr
    description: NonEmptyStr
    input_schema: FrozenJsonObjectValue
    read_only: bool
    idempotent: bool
    risk: ToolRisk
    capabilities: tuple[NonEmptyStr, ...] = ()
    approval_required: bool


class ToolContext(FrozenModel):
    invocation_id: UUID
    conversation: ConversationKey
    actor_identity_id: NonEmptyStr
    granted_capabilities: tuple[NonEmptyStr, ...] = ()
    correlation_id: NonEmptyStr


class ToolError(FrozenModel):
    code: NonEmptyStr
    message: NonEmptyStr
    retryable: bool = False
    details: FrozenJsonObjectValue = Field(
        default_factory=empty_frozen_json_object,
        validate_default=False,
    )


class ToolResult(FrozenModel):
    ok: bool
    data: FrozenJsonObjectValue | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.ok and (self.data is None or self.error is not None):
            raise ValueError("successful tool results require data and forbid error")
        if not self.ok and (self.data is not None or self.error is None):
            raise ValueError("failed tool results require error and forbid data")
        return self

    @classmethod
    def success(cls, data: dict[str, JsonValue]) -> Self:
        return cls.model_validate({"ok": True, "data": data})

    @classmethod
    def failure(cls, error: ToolError) -> Self:
        return cls(ok=False, error=error)
