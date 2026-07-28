"""Repository-backed evaluation cases and deterministic assertions."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from mybot.adapters.payload import as_string_mapping
from mybot.contracts import (
    EvaluationAssertion,
    EvaluationCase,
    EvaluationOutcome,
)
from mybot.infrastructure.llm import ChatMessage, LlmReply
from mybot.repositories.safety import EvaluationRepository

_CASE_LIST = TypeAdapter(list[EvaluationCase])
_SAFE_CASE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(slots=True)
class EvaluationCaseStore:
    root: Path

    def cases(self) -> tuple[EvaluationCase, ...]:
        root = self._root()
        cases: list[EvaluationCase] = []
        for path in sorted(root.glob("*.json")):
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            cases.extend(_CASE_LIST.validate_python(json.loads(path.read_text("utf-8"))))
        seen: set[str] = set()
        unique: list[EvaluationCase] = []
        for case in cases:
            if case.id in seen:
                raise ValueError(f"duplicate evaluation case id: {case.id}")
            seen.add(case.id)
            unique.append(case)
        return tuple(unique)

    def append(self, case: EvaluationCase, *, filename: str = "regression.json") -> Path:
        root = self._root()
        target = (root / Path(filename).name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("evaluation case path escapes the configured root")
        existing = (
            _CASE_LIST.validate_python(json.loads(target.read_text("utf-8")))
            if target.exists()
            else []
        )
        slug = _SAFE_CASE_ID.sub("-", case.id).strip("-")
        normalized = case.model_copy(update={"id": slug or "feedback-case"})
        by_id = {item.id: item for item in existing}
        by_id[normalized.id] = normalized
        payload = [item.model_dump(mode="json") for item in by_id.values()]
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
        return target

    def _root(self) -> Path:
        root = self.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root


def evaluate_assertions(
    case: EvaluationCase,
    *,
    response: str,
    tool_calls: tuple[str, ...] = (),
    citations: tuple[str, ...] = (),
) -> EvaluationOutcome:
    assertions: list[EvaluationAssertion] = []
    folded = response.casefold()
    for expected in case.expected.must_contain:
        assertions.append(
            EvaluationAssertion(
                kind="must_contain",
                expected=expected,
                actual=response,
                passed=expected.casefold() in folded,
            )
        )
    for expected in case.expected.must_not_contain:
        assertions.append(
            EvaluationAssertion(
                kind="must_not_contain",
                expected=expected,
                actual=response,
                passed=expected.casefold() not in folded,
            )
        )
    for expected in case.expected.must_call_tool:
        assertions.append(
            EvaluationAssertion(
                kind="must_call_tool",
                expected=expected,
                actual=cast(JsonValue, list(tool_calls)),
                passed=expected in tool_calls,
            )
        )
    if case.expected.must_cite:
        assertions.append(
            EvaluationAssertion(
                kind="must_cite",
                expected=True,
                actual=cast(JsonValue, list(citations)),
                passed=bool(citations),
            )
        )
    return EvaluationOutcome(
        passed=all(assertion.passed for assertion in assertions),
        assertions=tuple(assertions),
    )


def run_offline_cases(store: EvaluationCaseStore) -> tuple[int, int]:
    passed = 0
    failed = 0
    for case in store.cases():
        if case.offline_response is None:
            continue
        outcome = evaluate_assertions(
            case,
            response=case.offline_response,
            tool_calls=case.offline_tool_calls,
            citations=case.offline_citations,
        )
        if outcome.passed:
            passed += 1
        else:
            failed += 1
    return passed, failed


class JudgeClient(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


@dataclass(slots=True)
class ShadowEvaluationSink:
    repository: EvaluationRepository
    judge: JudgeClient | None = None

    async def complete_shadow(
        self,
        *,
        result_id: UUID,
        conversation_id: UUID,
        inbound_message_id: UUID,
        response: str,
        citations: Sequence[str],
    ) -> None:
        context = await self.repository.shadow_context(
            result_id,
            conversation_id=conversation_id,
            inbound_message_id=inbound_message_id,
        )
        outcome = evaluate_assertions(
            context.case,
            response=response,
            tool_calls=context.tool_calls,
            citations=tuple(citations),
        )
        judge_score: float | None = None
        judge_reason: str | None = None
        if context.judge_enabled and self.judge is not None:
            try:
                reply = await self.judge.complete(
                    [
                        ChatMessage(
                            role="system",
                            content=(
                                "Score the answer from 0 to 1. Return strict JSON with "
                                "keys score and reason."
                            ),
                        ),
                        ChatMessage(
                            role="user",
                            content=json.dumps(
                                {
                                    "question": context.case.question,
                                    "expected": context.case.expected.model_dump(mode="json"),
                                    "answer": response,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ]
                )
                parsed = as_string_mapping(json.loads(reply.text))
                if parsed is not None:
                    raw_score = parsed.get("score")
                    if isinstance(raw_score, (int, float)):
                        judge_score = max(0.0, min(float(raw_score), 1.0))
                    raw_reason = parsed.get("reason")
                    if isinstance(raw_reason, str):
                        judge_reason = raw_reason[:2_000]
            except Exception as error:
                judge_reason = f"judge_error:{type(error).__name__}"
        await self.repository.complete(
            result_id,
            response=response,
            citations=citations,
            context=context,
            outcome=outcome,
            judge_score=judge_score,
            judge_reason=judge_reason,
        )


__all__ = [
    "EvaluationCaseStore",
    "ShadowEvaluationSink",
    "evaluate_assertions",
    "run_offline_cases",
]
