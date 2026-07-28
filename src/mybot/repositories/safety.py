"""Persistence for moderation, per-call approvals, evaluations, and feedback."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import (
    EvaluationCase,
    EvaluationOutcome,
    MessageFeedbackRating,
    ModerationDecision,
    ModerationRequest,
)
from mybot.repositories import (
    evaluation_result_table,
    evaluation_run_table,
    llm_call_log_table,
    message_feedback_table,
    messages_table,
    moderation_audit_table,
    operator_audit_table,
    tool_approval_request_table,
    tool_invocations_table,
    turns_table,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class ModerationAuditRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self,
        request: ModerationRequest,
        decision: ModerationDecision,
        *,
        content_sha256: str,
        content_preview: str,
        duration_ms: int,
        conversation_stable_key: str | None = None,
        message_id: UUID | None = None,
        trace_id: str | None = None,
        error_code: str | None = None,
    ) -> UUID:
        audit_id = uuid4()
        async with self.sessions() as session:
            await session.execute(
                sa.insert(moderation_audit_table).values(
                    id=audit_id,
                    point=request.point.value,
                    backend=decision.backend,
                    flagged=decision.flagged,
                    action=decision.action.value,
                    preset_response=decision.preset_response or None,
                    reason=decision.reason,
                    matched_terms=list(decision.matched_terms),
                    content_sha256=content_sha256,
                    content_preview=content_preview,
                    conversation_stable_key=conversation_stable_key,
                    message_id=message_id,
                    trace_id=trace_id,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    created_at=_utc_now(),
                )
            )
            await session.commit()
        return audit_id

    async def recent(self, *, limit: int = 100) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(moderation_audit_table)
                    .order_by(moderation_audit_table.c.created_at.desc())
                    .limit(limit)
                )
            ).mappings()
            return [_json_row(row) for row in rows]


@dataclass(slots=True, frozen=True)
class ApprovalDecision:
    status: str
    note: str = ""


@dataclass(slots=True)
class ToolApprovalRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def request(
        self,
        *,
        invocation_id: UUID,
        tool_id: str,
        conversation_stable_key: str,
        actor_identity_id: str,
        correlation_id: str,
        arguments: Mapping[str, JsonValue],
        timeout_seconds: float,
    ) -> None:
        now = _utc_now()
        async with self.sessions() as session:
            await session.execute(
                pg_insert(tool_approval_request_table)
                .values(
                    id=invocation_id,
                    tool_id=tool_id,
                    conversation_stable_key=conversation_stable_key,
                    actor_identity_id=actor_identity_id,
                    correlation_id=correlation_id,
                    arguments=dict(arguments),
                    status="PENDING",
                    requested_at=now,
                    expires_at=now + timedelta(seconds=timeout_seconds),
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                sa.insert(operator_audit_table).values(
                    id=uuid4(),
                    action="tool.approval.requested",
                    detail={
                        "invocation_id": str(invocation_id),
                        "tool_id": tool_id,
                        "conversation_stable_key": conversation_stable_key,
                    },
                )
            )
            await session.commit()

    async def wait(
        self,
        invocation_id: UUID,
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.25,
    ) -> ApprovalDecision:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            decision = await self.status(invocation_id)
            if decision.status != "PENDING":
                return decision
            if asyncio.get_running_loop().time() >= deadline:
                return await self.expire(invocation_id)
            await asyncio.sleep(poll_seconds)

    async def status(self, invocation_id: UUID) -> ApprovalDecision:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.select(
                        tool_approval_request_table.c.status,
                        tool_approval_request_table.c.decision_note,
                        tool_approval_request_table.c.expires_at,
                    ).where(tool_approval_request_table.c.id == invocation_id)
                )
            ).one_or_none()
        if row is None:
            return ApprovalDecision("EXPIRED", "approval request disappeared")
        if row.status == "PENDING" and row.expires_at <= _utc_now():
            return await self.expire(invocation_id)
        return ApprovalDecision(str(row.status), str(row.decision_note or ""))

    async def decide(
        self, invocation_id: UUID, *, approved: bool, note: str = ""
    ) -> bool:
        now = _utc_now()
        status = "APPROVED" if approved else "REJECTED"
        async with self.sessions() as session:
            result = await session.execute(
                sa.update(tool_approval_request_table)
                .where(
                    tool_approval_request_table.c.id == invocation_id,
                    tool_approval_request_table.c.status == "PENDING",
                    tool_approval_request_table.c.expires_at > now,
                )
                .values(status=status, decision_note=note, decided_at=now)
            )
            changed = bool(getattr(result, "rowcount", 0) == 1)
            if changed:
                await session.execute(
                    sa.insert(operator_audit_table).values(
                        id=uuid4(),
                        action=f"tool.approval.{status.lower()}",
                        detail={"invocation_id": str(invocation_id), "note": note},
                    )
                )
            await session.commit()
        return changed

    async def expire(self, invocation_id: UUID) -> ApprovalDecision:
        now = _utc_now()
        async with self.sessions() as session:
            result = await session.execute(
                sa.update(tool_approval_request_table)
                .where(
                    tool_approval_request_table.c.id == invocation_id,
                    tool_approval_request_table.c.status == "PENDING",
                )
                .values(status="EXPIRED", decided_at=now)
            )
            if getattr(result, "rowcount", 0) == 1:
                await session.execute(
                    sa.insert(operator_audit_table).values(
                        id=uuid4(),
                        action="tool.approval.expired",
                        detail={"invocation_id": str(invocation_id)},
                    )
                )
            await session.commit()
        return ApprovalDecision("EXPIRED", "approval timed out")

    async def pending(self, *, limit: int = 100) -> list[dict[str, JsonValue]]:
        now = _utc_now()
        async with self.sessions() as session:
            expired = (
                await session.execute(
                sa.update(tool_approval_request_table)
                .where(
                    tool_approval_request_table.c.status == "PENDING",
                    tool_approval_request_table.c.expires_at <= now,
                )
                .values(status="EXPIRED", decided_at=now)
                .returning(tool_approval_request_table.c.id)
                )
            ).scalars().all()
            if expired:
                await session.execute(
                    sa.insert(operator_audit_table),
                    [
                        {
                            "id": uuid4(),
                            "action": "tool.approval.expired",
                            "detail": {"invocation_id": str(invocation_id)},
                        }
                        for invocation_id in expired
                    ],
                )
            rows = (
                await session.execute(
                    sa.select(tool_approval_request_table)
                    .where(tool_approval_request_table.c.status == "PENDING")
                    .order_by(tool_approval_request_table.c.requested_at.asc())
                    .limit(limit)
                )
            ).mappings().all()
            await session.commit()
        return [_json_row(row) for row in rows]


@dataclass(slots=True)
class FeedbackRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def save(
        self, message_id: UUID, *, rating: MessageFeedbackRating, note: str
    ) -> dict[str, JsonValue]:
        now = _utc_now()
        async with self.sessions() as session:
            direction = (
                await session.execute(
                    sa.select(messages_table.c.direction).where(messages_table.c.id == message_id)
                )
            ).scalar_one_or_none()
            if direction != "outbound":
                raise LookupError("feedback is only allowed for bot messages")
            await session.execute(
                pg_insert(message_feedback_table)
                .values(
                    id=uuid4(),
                    message_id=message_id,
                    rating=rating.value,
                    note=note,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["message_id"],
                    set_={"rating": rating.value, "note": note, "updated_at": now},
                )
            )
            await session.execute(
                sa.insert(operator_audit_table).values(
                    id=uuid4(),
                    action="message.feedback",
                    detail={"message_id": str(message_id), "rating": rating.value},
                )
            )
            row = (
                await session.execute(
                    sa.select(message_feedback_table).where(
                        message_feedback_table.c.message_id == message_id
                    )
                )
            ).mappings().one()
            await session.commit()
        return _json_row(row)

    async def metrics(self, *, hours: int = 24 * 30) -> dict[str, JsonValue]:
        since = _utc_now() - timedelta(hours=hours)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(
                        message_feedback_table.c.rating,
                        sa.func.count().label("count"),
                    )
                    .where(message_feedback_table.c.updated_at >= since)
                    .group_by(message_feedback_table.c.rating)
                )
            ).all()
        counts = {str(rating): int(count) for rating, count in rows}
        positive = counts.get("POSITIVE", 0)
        negative = counts.get("NEGATIVE", 0)
        total = positive + negative
        return {
            "positive": positive,
            "negative": negative,
            "total": total,
            "negative_rate": round(negative / total, 4) if total else 0.0,
        }

    async def by_message(self, message_id: UUID) -> dict[str, JsonValue] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.select(message_feedback_table).where(
                        message_feedback_table.c.message_id == message_id
                    )
                )
            ).mappings().one_or_none()
        return _json_row(row) if row is not None else None


@dataclass(slots=True, frozen=True)
class ShadowEvaluationContext:
    case: EvaluationCase
    judge_enabled: bool
    tool_calls: tuple[str, ...]
    model_channel: str | None
    model: str | None


@dataclass(slots=True)
class EvaluationRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def create_run(
        self,
        *,
        name: str,
        dataset_path: str,
        cases: Sequence[EvaluationCase],
        profile_id: UUID | None,
        persona_version_id: UUID | None,
        model_channel: str | None,
        judge_enabled: bool,
    ) -> tuple[UUID, tuple[tuple[UUID, EvaluationCase], ...]]:
        run_id = uuid4()
        now = _utc_now()
        queued = tuple((uuid4(), case) for case in cases)
        async with self.sessions() as session:
            await session.execute(
                sa.insert(evaluation_run_table).values(
                    id=run_id,
                    name=name,
                    status="QUEUED",
                    dataset_path=dataset_path,
                    profile_id=profile_id,
                    persona_version_id=persona_version_id,
                    model_channel=model_channel,
                    judge_enabled=judge_enabled,
                    total=len(cases),
                    completed=0,
                    passed=0,
                    failed=0,
                    created_at=now,
                )
            )
            if queued:
                await session.execute(
                    sa.insert(evaluation_result_table),
                    [
                        {
                            "id": result_id,
                            "run_id": run_id,
                            "case_id": case.id,
                            "case_snapshot": case.model_dump(mode="json"),
                            "status": "QUEUED",
                            "tool_calls": [],
                            "citations": [],
                            "assertions": [],
                            "created_at": now,
                        }
                        for result_id, case in queued
                    ],
                )
            await session.commit()
        return run_id, queued

    async def attach_conversation(
        self, result_id: UUID, conversation_id: UUID
    ) -> None:
        async with self.sessions() as session:
            await session.execute(
                sa.update(evaluation_result_table)
                .where(evaluation_result_table.c.id == result_id)
                .values(conversation_id=conversation_id, status="RUNNING")
            )
            run_id = (
                await session.execute(
                    sa.select(evaluation_result_table.c.run_id).where(
                        evaluation_result_table.c.id == result_id
                    )
                )
            ).scalar_one()
            await session.execute(
                sa.update(evaluation_run_table)
                .where(evaluation_run_table.c.id == run_id)
                .values(status="RUNNING")
            )
            await session.commit()

    async def shadow_context(
        self,
        result_id: UUID,
        *,
        conversation_id: UUID,
        inbound_message_id: UUID,
    ) -> ShadowEvaluationContext:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.select(
                        evaluation_result_table.c.case_snapshot,
                        evaluation_run_table.c.judge_enabled,
                    )
                    .join(
                        evaluation_run_table,
                        evaluation_run_table.c.id == evaluation_result_table.c.run_id,
                    )
                    .where(evaluation_result_table.c.id == result_id)
                )
            ).one()
            tool_rows = (
                await session.execute(
                    sa.select(tool_invocations_table.c.tool_id)
                    .join(turns_table, turns_table.c.id == tool_invocations_table.c.turn_id)
                    .where(turns_table.c.inbound_message_id == inbound_message_id)
                    .order_by(tool_invocations_table.c.created_at.asc())
                )
            ).all()
            model_row = (
                await session.execute(
                    sa.select(llm_call_log_table.c.channel, llm_call_log_table.c.model)
                    .where(llm_call_log_table.c.conversation_id == conversation_id)
                    .order_by(llm_call_log_table.c.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
        return ShadowEvaluationContext(
            case=EvaluationCase.model_validate(row.case_snapshot),
            judge_enabled=bool(row.judge_enabled),
            tool_calls=tuple(str(item.tool_id) for item in tool_rows),
            model_channel=str(model_row.channel) if model_row is not None else None,
            model=str(model_row.model) if model_row is not None else None,
        )

    async def complete(
        self,
        result_id: UUID,
        *,
        response: str,
        citations: Sequence[str],
        context: ShadowEvaluationContext,
        outcome: EvaluationOutcome,
        judge_score: float | None = None,
        judge_reason: str | None = None,
    ) -> None:
        now = _utc_now()
        async with self.sessions() as session:
            run_id = (
                await session.execute(
                    sa.select(evaluation_result_table.c.run_id).where(
                        evaluation_result_table.c.id == result_id
                    )
                )
            ).scalar_one()
            await session.execute(
                sa.update(evaluation_result_table)
                .where(evaluation_result_table.c.id == result_id)
                .values(
                    status="COMPLETED",
                    response=response,
                    tool_calls=list(context.tool_calls),
                    citations=list(citations),
                    assertions=[
                        assertion.model_dump(mode="json")
                        for assertion in outcome.assertions
                    ],
                    passed=outcome.passed,
                    judge_score=judge_score,
                    judge_reason=judge_reason,
                    model_channel=context.model_channel,
                    model=context.model,
                    finished_at=now,
                )
            )
            counts = (
                await session.execute(
                    sa.select(
                        sa.func.count().filter(
                            evaluation_result_table.c.status == "COMPLETED"
                        ),
                        sa.func.count().filter(evaluation_result_table.c.passed.is_(True)),
                        sa.func.count().filter(evaluation_result_table.c.passed.is_(False)),
                    ).where(evaluation_result_table.c.run_id == run_id)
                )
            ).one()
            completed, passed, failed = (int(value or 0) for value in counts)
            total = (
                await session.execute(
                    sa.select(evaluation_run_table.c.total).where(
                        evaluation_run_table.c.id == run_id
                    )
                )
            ).scalar_one()
            await session.execute(
                sa.update(evaluation_run_table)
                .where(evaluation_run_table.c.id == run_id)
                .values(
                    completed=completed,
                    passed=passed,
                    failed=failed,
                    status="COMPLETED" if completed >= total else "RUNNING",
                    finished_at=now if completed >= total else None,
                )
            )
            await session.commit()

    async def list_runs(self, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(evaluation_run_table)
                    .order_by(evaluation_run_table.c.created_at.desc())
                    .limit(limit)
                )
            ).mappings().all()
        return [_json_row(row) for row in rows]

    async def run_detail(self, run_id: UUID) -> dict[str, JsonValue] | None:
        async with self.sessions() as session:
            run = (
                await session.execute(
                    sa.select(evaluation_run_table).where(evaluation_run_table.c.id == run_id)
                )
            ).mappings().one_or_none()
            if run is None:
                return None
            rows = (
                await session.execute(
                    sa.select(evaluation_result_table)
                    .where(evaluation_result_table.c.run_id == run_id)
                    .order_by(evaluation_result_table.c.case_id.asc())
                )
            ).mappings().all()
        return {
            "run": cast(JsonValue, _json_row(run)),
            "results": cast(JsonValue, [_json_row(row) for row in rows]),
        }


def _json_row(row: RowMapping) -> dict[str, JsonValue]:
    return {
        str(key): cast(
            JsonValue,
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else value,
        )
        for key, value in row.items()
    }


__all__ = [
    "ApprovalDecision",
    "EvaluationRepository",
    "FeedbackRepository",
    "ModerationAuditRepository",
    "ShadowEvaluationContext",
    "ToolApprovalRepository",
]
