"""Read-only aggregate queries backing the operator console."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class OperatorViews:
    sessions: async_sessionmaker[AsyncSession]

    async def conversations(self, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT c.id, c.stable_key, c.platform, c.chat_kind, c.chat_id,
                               c.ephemeral, c.last_message_at,
                               (SELECT count(*) FROM messages m
                                WHERE m.conversation_id = c.id) AS message_count
                        FROM conversations c
                        ORDER BY c.last_message_at DESC NULLS LAST, c.created_at DESC
                        LIMIT :limit
                        """
                        ),
                        {"limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "id": str(row["id"]),
                    "stable_key": row["stable_key"],
                    "platform": row["platform"],
                    "chat_kind": row["chat_kind"],
                    "chat_id": row["chat_id"],
                    "ephemeral": bool(row["ephemeral"]),
                    "last_message_at": _iso(row["last_message_at"]),
                    "message_count": int(row["message_count"]),
                }
                for row in rows
            ]

    async def messages(
        self, conversation_id: UUID, *, limit: int = 100
    ) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT direction, sender_identity_id, segments, occurred_at, trace_id
                        FROM messages
                        WHERE conversation_id = :conversation_id
                        ORDER BY occurred_at ASC
                        LIMIT :limit
                        """
                        ),
                        {"conversation_id": conversation_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "direction": row["direction"],
                    "sender_identity_id": row["sender_identity_id"],
                    "text": _texts(row["segments"]),
                    "occurred_at": _iso(row["occurred_at"]),
                    "trace_id": row["trace_id"],
                    "segments": cast(JsonValue, row["segments"]),
                }
                for row in rows
            ]

    async def conversation_by_stable_key(
        self, stable_key: str
    ) -> dict[str, JsonValue] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, stable_key, platform, chat_kind, chat_id,
                               ephemeral, last_message_at
                        FROM conversations
                        WHERE stable_key = :stable_key
                        """
                    ),
                    {"stable_key": stable_key},
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            return {
                "id": str(row["id"]),
                "stable_key": row["stable_key"],
                "platform": row["platform"],
                "chat_kind": row["chat_kind"],
                "chat_id": row["chat_id"],
                "ephemeral": bool(row["ephemeral"]),
                "last_message_at": _iso(row["last_message_at"]),
            }

    async def traces(
        self,
        conversation_id: UUID,
        *,
        trace_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, JsonValue]]:
        clauses = ["conversation_id = :conversation_id"]
        params: dict[str, object] = {
            "conversation_id": conversation_id,
            "limit": limit,
        }
        if trace_id is not None:
            clauses.append("trace_id = :trace_id")
            params["trace_id"] = trace_id
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"""
                        SELECT id, trace_id, message_id, stage, status,
                               duration_ms, attributes, created_at
                        FROM trace_spans
                        WHERE {' AND '.join(clauses)}
                        ORDER BY created_at ASC, id ASC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "trace_id": row["trace_id"],
                    "message_id": str(row["message_id"]) if row["message_id"] else None,
                    "stage": row["stage"],
                    "status": row["status"],
                    "duration_ms": int(row["duration_ms"]),
                    "attributes": cast(JsonValue, row["attributes"]),
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ]

    async def turns(self, conversation_id: UUID, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            turn_rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT id, action, trigger, model, prompt_tokens,
                               completion_tokens, latency_ms, outcome, error, created_at
                        FROM turns
                        WHERE conversation_id = :conversation_id
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                        ),
                        {"conversation_id": conversation_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            turn_ids = [row["id"] for row in turn_rows]
            invocations: dict[UUID, list[JsonValue]] = {}
            if turn_ids:
                inv_rows = (
                    (
                        await session.execute(
                            sa.text(
                                """
                            SELECT turn_id, tool_id, ok, error_code, latency_ms
                            FROM tool_invocations
                            WHERE turn_id = ANY(:turn_ids)
                            ORDER BY created_at ASC
                            """
                            ),
                            {"turn_ids": turn_ids},
                        )
                    )
                    .mappings()
                    .all()
                )
                for inv in inv_rows:
                    invocations.setdefault(inv["turn_id"], []).append(
                        {
                            "tool_id": inv["tool_id"],
                            "ok": inv["ok"],
                            "error_code": inv["error_code"],
                            "latency_ms": int(inv["latency_ms"]),
                        }
                    )
            return [
                {
                    "id": str(row["id"]),
                    "action": row["action"],
                    "trigger": row["trigger"],
                    "model": row["model"],
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "latency_ms": int(row["latency_ms"]),
                    "outcome": row["outcome"],
                    "error": row["error"],
                    "created_at": _iso(row["created_at"]),
                    "tool_invocations": invocations.get(row["id"], []),
                }
                for row in turn_rows
            ]

    async def memories(
        self,
        *,
        scope: str | None = None,
        include_revoked: bool = False,
        state: str = "active",
        limit: int = 100,
    ) -> list[dict[str, JsonValue]]:
        clauses = ["1 = 1"]
        params: dict[str, JsonValue] = {"limit": limit}
        if scope is not None:
            clauses.append("scope = :scope")
            params["scope"] = scope
        if include_revoked:
            state = "all"
        if state == "active":
            clauses.extend(["revoked_at IS NULL", "invalid_at IS NULL"])
        elif state == "invalidated":
            clauses.append("invalid_at IS NOT NULL AND revoked_at IS NULL")
        elif state == "revoked":
            clauses.append("revoked_at IS NOT NULL")
        elif state != "all":
            raise ValueError("memory state must be active, invalidated, revoked, or all")
        where = " AND ".join(clauses)
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            f"""
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               kind, content, confidence, relationship_score,
                               privacy, revoked_at,
                               revoked_reason, invalid_at, invalidated_by, supersedes,
                               created_at, last_accessed_at
                        FROM memory_items
                        WHERE {where}
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "id": str(row["id"]),
                    "scope": row["scope"],
                    "subject_identity_id": row["subject_identity_id"],
                    "conversation_stable_key": row["conversation_stable_key"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "confidence": float(row["confidence"]),
                    "relationship_score": (
                        float(row["relationship_score"])
                        if row["relationship_score"] is not None
                        else None
                    ),
                    "privacy": row["privacy"],
                    "revoked_at": _iso(row["revoked_at"]),
                    "revoked_reason": row["revoked_reason"],
                    "invalid_at": _iso(row["invalid_at"]),
                    "invalidated_by": (
                        str(row["invalidated_by"])
                        if row["invalidated_by"] is not None
                        else None
                    ),
                    "supersedes": cast(JsonValue, row["supersedes"]),
                    "state": (
                        "revoked"
                        if row["revoked_at"] is not None
                        else "invalidated"
                        if row["invalid_at"] is not None
                        else "active"
                    ),
                    "created_at": _iso(row["created_at"]),
                    "last_accessed_at": _iso(row["last_accessed_at"]),
                }
                for row in rows
            ]

    async def memory_history(self, memory_id: UUID) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            target = (
                await session.execute(
                    sa.text(
                        """
                        SELECT scope, subject_identity_id, conversation_stable_key,
                               privacy
                        FROM memory_items WHERE id = :id
                        """
                    ),
                    {"id": memory_id},
                )
            ).mappings().one_or_none()
            if target is None:
                return []
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               kind, content, confidence, relationship_score,
                               privacy, supersedes,
                               invalid_at, invalidated_by, revoked_at, revoked_reason,
                               created_at
                        FROM memory_items
                        WHERE scope = :scope AND privacy = :privacy
                          AND subject_identity_id IS NOT DISTINCT FROM :subject
                          AND conversation_stable_key IS NOT DISTINCT FROM :conversation
                        ORDER BY created_at ASC, id ASC
                        LIMIT 500
                        """
                    ),
                    {
                        "scope": target["scope"],
                        "privacy": target["privacy"],
                        "subject": target["subject_identity_id"],
                        "conversation": target["conversation_stable_key"],
                    },
                )
            ).mappings().all()
        connected = _connected_memory_ids(rows, memory_id)
        return [
            {
                "id": str(row["id"]),
                "scope": row["scope"],
                "subject_identity_id": row["subject_identity_id"],
                "conversation_stable_key": row["conversation_stable_key"],
                "kind": row["kind"],
                "content": row["content"],
                "confidence": float(row["confidence"]),
                "relationship_score": (
                    float(row["relationship_score"])
                    if row["relationship_score"] is not None
                    else None
                ),
                "privacy": row["privacy"],
                "supersedes": cast(JsonValue, row["supersedes"]),
                "invalid_at": _iso(row["invalid_at"]),
                "invalidated_by": (
                    str(row["invalidated_by"])
                    if row["invalidated_by"] is not None
                    else None
                ),
                "revoked_at": _iso(row["revoked_at"]),
                "revoked_reason": row["revoked_reason"],
                "state": (
                    "revoked"
                    if row["revoked_at"] is not None
                    else "invalidated"
                    if row["invalid_at"] is not None
                    else "active"
                ),
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
            if row["id"] in connected
        ]

    async def relationships(self, *, limit: int = 200) -> list[dict[str, JsonValue]]:
        """List active relationship summaries, one current version per subject."""

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT DISTINCT ON (subject_identity_id)
                               id, subject_identity_id, content, confidence,
                               relationship_score, source_message_ids, created_at
                        FROM memory_items
                        WHERE scope = 'SUBJECT'
                          AND kind = 'RELATIONSHIP'
                          AND revoked_at IS NULL
                          AND invalid_at IS NULL
                        ORDER BY subject_identity_id, created_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
        return [
            {
                "id": str(row["id"]),
                "subject_identity_id": row["subject_identity_id"],
                "impression": row["content"],
                "familiarity": float(row["relationship_score"] or 0.0),
                "confidence": float(row["confidence"]),
                "source_message_ids": cast(JsonValue, row["source_message_ids"]),
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ]

    async def memory_operations(
        self, memory_id: UUID, *, limit: int = 100
    ) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, operation, source, memory_id, previous_memory_id,
                               detail, created_at
                        FROM memory_operation_audit
                        WHERE memory_id = :id OR previous_memory_id = :id
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"id": memory_id, "limit": limit},
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "operation": row["operation"],
                    "source": row["source"],
                    "memory_id": str(row["memory_id"]) if row["memory_id"] else None,
                    "previous_memory_id": (
                        str(row["previous_memory_id"])
                        if row["previous_memory_id"]
                        else None
                    ),
                    "detail": cast(JsonValue, row["detail"]),
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ]

    async def core_blocks(self, *, limit: int = 200) -> list[dict[str, JsonValue]]:
        from mybot.repositories.core_memory import CoreBlockRepository

        records = await CoreBlockRepository(self.sessions).list_all(limit=limit)
        return [
            {
                **record.block.model_dump(mode="json"),
                "id": str(record.block.id),
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            }
            for record in records
        ]

    async def replace_core_block(
        self,
        *,
        label: str,
        subject_identity_id: str | None,
        content: str,
        token_budget: int,
    ) -> dict[str, JsonValue]:
        from mybot.contracts import CoreBlockLabel
        from mybot.repositories.core_memory import CoreBlockRepository

        record = await CoreBlockRepository(self.sessions).replace(
            label=CoreBlockLabel(label),
            subject_identity_id=subject_identity_id,
            content=content,
            token_budget=token_budget,
            source="operator",
        )
        return {
            **record.block.model_dump(mode="json"),
            "id": str(record.block.id),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    async def revoke_memory(self, memory_id: UUID) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                sa.text(
                    "UPDATE memory_items SET revoked_at = now(), "
                    "revoked_reason = 'operator' "
                    "WHERE id = :id AND revoked_at IS NULL"
                ),
                {"id": memory_id},
            )
            await session.commit()
            return bool(getattr(result, "rowcount", 0))

    async def usage(self, *, days: int = 14) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT date_trunc('day', created_at) AS day,
                               count(*) AS turns,
                               coalesce(sum(prompt_tokens + completion_tokens), 0) AS tokens
                        FROM turns
                        WHERE created_at >= now() - make_interval(days => :days)
                        GROUP BY day
                        ORDER BY day ASC
                        """
                        ),
                        {"days": days},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "day": row["day"].date().isoformat(),
                    "turns": int(row["turns"]),
                    "tokens": int(row["tokens"]),
                }
                for row in rows
            ]

    async def model_channel_usage(self, *, days: int = 30) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        WITH latest AS (
                            SELECT DISTINCT ON (channel)
                                   channel, status, created_at
                            FROM llm_call_log
                            ORDER BY channel, created_at DESC
                        )
                        SELECT l.channel,
                               count(*) AS calls,
                               coalesce(sum(l.prompt_tokens), 0) AS prompt_tokens,
                               coalesce(sum(l.completion_tokens), 0) AS completion_tokens,
                               coalesce(sum(l.cost_usd_micros), 0) AS cost_usd_micros,
                               latest.status AS last_status,
                               latest.created_at AS last_called_at
                        FROM llm_call_log l
                        JOIN latest ON latest.channel = l.channel
                        WHERE l.created_at >= now() - make_interval(days => :days)
                        GROUP BY l.channel, latest.status, latest.created_at
                        ORDER BY l.channel
                        """
                        ),
                        {"days": days},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "channel": row["channel"],
                    "calls": int(row["calls"]),
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "cost_usd_micros": int(row["cost_usd_micros"]),
                    "last_status": row["last_status"],
                    "last_called_at": _iso(row["last_called_at"]),
                }
                for row in rows
            ]

    async def model_daily_usage(self, *, days: int = 30) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT date_trunc('day', created_at) AS day,
                               count(*) AS calls,
                               coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                               coalesce(sum(completion_tokens), 0) AS completion_tokens,
                               coalesce(sum(cost_usd_micros), 0) AS cost_usd_micros
                        FROM llm_call_log
                        WHERE created_at >= now() - make_interval(days => :days)
                        GROUP BY day
                        ORDER BY day ASC
                        """
                        ),
                        {"days": days},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "day": row["day"].date().isoformat(),
                    "calls": int(row["calls"]),
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "cost_usd_micros": int(row["cost_usd_micros"]),
                }
                for row in rows
            ]

    async def model_conversation_usage(
        self, *, days: int = 30, limit: int = 10
    ) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT l.conversation_id, c.stable_key,
                               count(*) AS calls,
                               coalesce(sum(l.prompt_tokens), 0) AS prompt_tokens,
                               coalesce(sum(l.completion_tokens), 0) AS completion_tokens,
                               coalesce(sum(l.cost_usd_micros), 0) AS cost_usd_micros
                        FROM llm_call_log l
                        LEFT JOIN conversations c ON c.id = l.conversation_id
                        WHERE l.created_at >= now() - make_interval(days => :days)
                        GROUP BY l.conversation_id, c.stable_key
                        ORDER BY cost_usd_micros DESC, calls DESC
                        LIMIT :limit
                        """
                        ),
                        {"days": days, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
            return [
                {
                    "conversation_id": (
                        str(row["conversation_id"])
                        if row["conversation_id"] is not None
                        else None
                    ),
                    "stable_key": row["stable_key"],
                    "calls": int(row["calls"]),
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "cost_usd_micros": int(row["cost_usd_micros"]),
                }
                for row in rows
            ]

    async def turn_metrics(self) -> dict[str, JsonValue]:
        async with self.sessions() as session:
            outcomes = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT outcome, count(*) AS n
                        FROM turns
                        WHERE created_at >= now() - interval '24 hours'
                        GROUP BY outcome
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            latency = (
                (
                    await session.execute(
                        sa.text(
                            """
                        SELECT coalesce(avg(latency_ms), 0) AS avg_ms,
                               coalesce(
                                 percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 0
                               ) AS p95_ms
                        FROM turns
                        WHERE created_at >= now() - interval '24 hours'
                          AND outcome IN ('replied', 'error')
                        """
                        )
                    )
                )
                .mappings()
                .one()
            )
            return {
                "by_outcome": {row["outcome"]: int(row["n"]) for row in outcomes},
                "avg_latency_ms": round(float(latency["avg_ms"]), 1),
                "p95_latency_ms": round(float(latency["p95_ms"]), 1),
            }


def _iso(value: object) -> JsonValue:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _texts(segments: object) -> str:
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for raw in cast(list[object], segments):
        if isinstance(raw, dict):
            entry = cast(dict[str, object], raw)
            if entry.get("type") == "text":
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _connected_memory_ids(rows: Sequence[RowMapping], start: UUID) -> set[UUID]:
    adjacency: dict[UUID, set[UUID]] = {}
    for row in rows:
        memory_id = cast(UUID, row["id"])
        adjacency.setdefault(memory_id, set())
        invalidated_by = row["invalidated_by"]
        if isinstance(invalidated_by, UUID):
            adjacency[memory_id].add(invalidated_by)
            adjacency.setdefault(invalidated_by, set()).add(memory_id)
        supersedes = row["supersedes"]
        if isinstance(supersedes, list):
            for raw in cast(list[object], supersedes):
                try:
                    predecessor = UUID(str(raw))
                except ValueError:
                    continue
                adjacency[memory_id].add(predecessor)
                adjacency.setdefault(predecessor, set()).add(memory_id)
    connected: set[UUID] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in connected:
            continue
        connected.add(current)
        pending.extend(adjacency.get(current, ()))
    return connected
