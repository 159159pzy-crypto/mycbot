import json

from mybot.contracts import EvaluationCase
from mybot.evaluation import EvaluationCaseStore, evaluate_assertions, run_offline_cases


def test_deterministic_assertions_cover_text_tools_and_citations() -> None:
    case = EvaluationCase(
        id="case",
        name="case",
        question="question",
        expected={
            "must_contain": ["answer"],
            "must_not_contain": ["secret"],
            "must_call_tool": ["kb_search"],
            "must_cite": True,
        },
    )

    outcome = evaluate_assertions(
        case,
        response="The ANSWER is here",
        tool_calls=("kb_search",),
        citations=("kb://doc/1",),
    )

    assert outcome.passed is True
    assert len(outcome.assertions) == 4


def test_case_store_rejects_duplicate_ids_and_can_append_feedback_case(tmp_path) -> None:
    store = EvaluationCaseStore(tmp_path)
    first = EvaluationCase(
        id="feedback 1",
        name="feedback",
        question="why",
        expected={"must_not_contain": ["bad"]},
    )

    path = store.append(first)

    assert path == tmp_path / "regression.json"
    payload = json.loads(path.read_text("utf-8"))
    assert payload[0]["id"] == "feedback-1"
    assert store.cases()[0].question == "why"


def test_repo_smoke_cases_pass_without_paid_tokens() -> None:
    passed, failed = run_offline_cases(EvaluationCaseStore(__import__("pathlib").Path("evals")))

    assert passed >= 1
    assert failed == 0
