import pytest
from pydantic import ValidationError

from mybot.contracts import (
    EvaluationCase,
    ModerationAction,
    ModerationDecision,
    ModerationPoint,
    ModerationRequest,
)


def test_moderation_contract_uses_the_roadmap_wire_shape() -> None:
    request = ModerationRequest(point=ModerationPoint.INBOUND, params={"text": "hello"})
    result = ModerationDecision(
        flagged=True,
        action=ModerationAction.DIRECT_OUTPUT,
        preset_response="blocked",
        backend="local",
    )

    assert request.model_dump(mode="json") == {
        "point": "inbound",
        "params": {"text": "hello"},
    }
    assert result.model_dump(mode="json")["action"] == "direct_output"


def test_evaluation_case_requires_at_least_one_deterministic_assertion() -> None:
    with pytest.raises(ValidationError):
        EvaluationCase(id="empty", name="empty", question="hello")

    case = EvaluationCase(
        id="citation",
        name="citation",
        question="quote the handbook",
        expected={"must_contain": ["handbook"], "must_cite": True},
    )

    assert case.expected.must_cite is True
    assert case.expected.must_contain == ("handbook",)
