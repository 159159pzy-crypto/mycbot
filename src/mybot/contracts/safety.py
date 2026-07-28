"""Content moderation, evaluation, feedback, and approval contracts."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.json import FrozenJsonObjectValue, empty_frozen_json_object


class ModerationPoint(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ModerationAction(StrEnum):
    DIRECT_OUTPUT = "direct_output"
    OVERRIDDEN = "overridden"


class ModerationRequest(FrozenModel):
    point: ModerationPoint
    params: FrozenJsonObjectValue = Field(
        default_factory=empty_frozen_json_object,
        validate_default=False,
    )


class ModerationDecision(FrozenModel):
    flagged: bool = False
    action: ModerationAction = ModerationAction.DIRECT_OUTPUT
    preset_response: str = ""
    backend: str = "none"
    reason: str = ""
    matched_terms: tuple[str, ...] = ()


class ToolApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class EvaluationExpected(FrozenModel):
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    must_call_tool: tuple[str, ...] = ()
    must_cite: bool = False


class EvaluationHistoryItem(FrozenModel):
    role: Literal["user", "assistant"]
    content: NonEmptyStr


class EvaluationCase(FrozenModel):
    id: NonEmptyStr
    name: NonEmptyStr
    question: NonEmptyStr
    history: tuple[EvaluationHistoryItem, ...] = ()
    expected: EvaluationExpected = Field(default_factory=EvaluationExpected)
    tags: tuple[NonEmptyStr, ...] = ()
    offline_response: str | None = None
    offline_tool_calls: tuple[str, ...] = ()
    offline_citations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_an_assertion(self) -> "EvaluationCase":
        expected = self.expected
        if not any(
            (
                expected.must_contain,
                expected.must_not_contain,
                expected.must_call_tool,
                expected.must_cite,
            )
        ):
            raise ValueError("evaluation cases require at least one assertion")
        return self


class EvaluationAssertion(FrozenModel):
    kind: NonEmptyStr
    expected: JsonValue
    passed: bool
    actual: JsonValue | None = None


class EvaluationOutcome(FrozenModel):
    passed: bool
    assertions: tuple[EvaluationAssertion, ...]


class MessageFeedbackRating(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class FeedbackCaseDraft(FrozenModel):
    feedback_id: UUID
    case: EvaluationCase


__all__ = [
    "EvaluationAssertion",
    "EvaluationCase",
    "EvaluationExpected",
    "EvaluationHistoryItem",
    "EvaluationOutcome",
    "FeedbackCaseDraft",
    "MessageFeedbackRating",
    "ModerationAction",
    "ModerationDecision",
    "ModerationPoint",
    "ModerationRequest",
    "ToolApprovalStatus",
]
