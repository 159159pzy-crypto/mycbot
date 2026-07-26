"""Read-only aggregate queries backing the operator console."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class OperatorViews:
    sessions: async_sessionmaker[AsyncSession]

    async def conversations(self, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT c.id, c.stable_key, c.platform, c.chat_kind, c.chat_id,
                               c.last_message_at,
                               (SELECT count(*) FROM messages m
                                WHERE m.conversation_id = c.id) AS message_count
                        FROM conversations c
                        ORDER BY c.last_message_at DESC NULLS LAST, c.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "stable_key": row["stable_key"],
                    "platform": row["platform"],
                    "chat_kind": row["chat_kind"],
                    "chat_id": row["chat_id"],
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
                await session.execute(
                    sa.text(
                        """
                        SELECT direction, sender_identity_id, segments, occurred_at
                        FROM messages
                        WHERE conversation_id = :conversation_id
                        ORDER BY occurred_at ASC
                        LIMIT :limit
                        """
                    ),
                    {"conversation_id": conversation_id, "limit": limit},
                )
            ).mappings().all()
            return [
                {
                    "direction": row["direction"],
                    "sender_identity_id": row["sender_identity_id"],
                    "text": _texts(row["segments"]),
                    "occurred_at": _iso(row["occurred_at"]),
                }
                for row in rows
            ]

    async def turns(
        self, conversation_id: UUID, *, limit: int = 50
    ) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            turn_rows = (
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
            ).mappings().all()
            turn_ids = [row["id"] for row in turn_rows]
            invocations: dict[UUID, list[JsonValue]] = {}
            if turn_ids:
                inv_rows = (
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
                ).mappings().all()
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
        limit: int = 100,
    ) -> list[dict[str, JsonValue]]:
        clauses = ["1 = 1"]
        params: dict[str, JsonValue] = {"limit": limit}
        if scope is not None:
            clauses.append("scope = :scope")
            params["scope"] = scope
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = " AND ".join(clauses)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"""
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               kind, content, confidence, privacy, revoked_at,
                               revoked_reason, created_at, last_accessed_at
                        FROM memory_items
                        WHERE {where}
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "scope": row["scope"],
                    "subject_identity_id": row["subject_identity_id"],
                    "conversation_stable_key": row["conversation_stable_key"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "confidence": float(row["confidence"]),
                    "privacy": row["privacy"],
                    "revoked_at": _iso(row["revoked_at"]),
                    "revoked_reason": row["revoked_reason"],
                    "created_at": _iso(row["created_at"]),
                    "last_accessed_at": _iso(row["last_accessed_at"]),
                }
                for row in rows
            ]

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
            ).mappings().all()
            return [
                {
                    "day": row["day"].date().isoformat(),
                    "turns": int(row["turns"]),
                    "tokens": int(row["tokens"]),
                }
                for row in rows
            ]

    async def turn_metrics(self) -> dict[str, JsonValue]:
        async with self.sessions() as session:
            outcomes = (
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
            ).mappings().all()
            latency = (
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
            ).mappings().one()
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
