import pytest
from pydantic import ValidationError

from mybot.contracts import (
    Citation,
    ReplyPlan,
    TurnAction,
    TurnDecision,
    TurnTrigger,
    TypingProfile,
)


def test_turn_decision_bounds_confidence() -> None:
    decision = TurnDecision(
        action=TurnAction.AGENT,
        reason="direct mention requires agent reasoning",
        confidence=0.91,
        trigger=TurnTrigger.MENTION,
    )

    assert decision.confidence == 0.91
    with pytest.raises(ValidationError):
        decision.model_copy(update={"confidence": 1.1}, deep=True)
        TurnDecision(
            action=TurnAction.IGNORE,
            reason="out of range",
            confidence=1.1,
            trigger=TurnTrigger.POLICY,
        )


def test_reply_plan_accepts_one_to_three_clean_segments() -> None:
    plan = ReplyPlan(
        text_segments=("  first update  ", "second update"),
        citations=(Citation(label="Runbook", uri="https://example.test/runbook"),),
        meme_intent="acknowledge",
        typing=TypingProfile(enabled=True, chars_per_second=14.0),
    )

    assert plan.text_segments == ("first update", "second update")
    assert plan.typing.enabled is True


@pytest.mark.parametrize(
    "segments",
    [(), ("",), ("valid", " "), ("one", "two", "three", "four")],
)
def test_reply_plan_rejects_empty_or_oversized_text_plans(segments: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        ReplyPlan(text_segments=segments)
