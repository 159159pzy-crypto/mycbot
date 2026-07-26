"""Scoped, revocable memory persistence over pgvector."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import MemoryItem, MemoryPrivacy, MemoryScope


@dataclass(slots=True, frozen=True)
class ScoredMemory:
    id: UUID
    scope: MemoryScope
    privacy: MemoryPrivacy
    kind: str
    content: str
    confidence: float
    created_at: datetime
    distance: float
    supersedes: tuple[UUID, ...]


@dataclass(slots=True, frozen=True)
class LifecycleReport:
    expired: int
    decayed: int
    deleted: int


def _vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"


def _rowcount(result: object) -> int:
    value = getattr(result, "rowcount", 0)
    if isinstance(value, int) and value > 0:
        return value
    return 0


@dataclass(slots=True)
class MemoryRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def store(
        self,
        item: MemoryItem,
        *,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
    ) -> UUID:
        async with self.sessions() as session:
            await session.execute(
                sa.text(
                    """
                    INSERT INTO memory_items (
                        id, scope, subject_identity_id, conversation_stable_key,
                        kind, content, source_message_ids, confidence, privacy,
                        valid_from, valid_until, conflicts_with, supersedes,
                        revoked_at, revoked_reason, embedding, embedding_model
                    ) VALUES (
                        :id, :scope, :subject, :conversation_key,
                        :kind, :content, CAST(:sources AS jsonb), :confidence, :privacy,
                        :valid_from, :valid_until, CAST(:conflicts AS jsonb),
                        CAST(:supersedes AS jsonb),
                        :revoked_at, :revoked_reason,
                        CAST(:embedding AS vector), :embedding_model
                    )
                    """
                ),
                {
                    "id": item.id,
                    "scope": item.scope.value,
                    "subject": item.subject_identity_id,
                    "conversation_key": (
                        item.conversation.stable_key if item.conversation else None
                    ),
                    "kind": item.kind,
                    "content": item.content,
                    "sources": json.dumps(list(item.source_message_ids)),
                    "confidence": item.confidence,
                    "privacy": item.privacy.value,
                    "valid_from": item.valid_from,
                    "valid_until": item.valid_until,
                    "conflicts": _uuid_json(item.conflicts_with),
                    "supersedes": _uuid_json(item.supersedes),
                    "revoked_at": item.revoked_at,
                    "revoked_reason": item.revoked_reason,
                    "embedding": (
                        _vector_literal(embedding) if embedding is not None else None
                    ),
                    "embedding_model": embedding_model,
                },
            )
            await session.commit()
        return item.id

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        subject_identity_id: str,
        conversation_stable_key: str,
        include_private: bool,
        now: datetime,
        limit: int = 5,
    ) -> tuple[ScoredMemory, ...]:
        """Cosine-nearest valid memories visible to this subject in this chat."""

        privacy_clause = (
            "" if include_private else "AND privacy NOT IN ('PRIVATE', 'SENSITIVE')"
        )
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"""
                        SELECT id, scope, privacy, kind, content, confidence,
                               created_at, supersedes,
                               embedding <=> CAST(:query AS vector) AS distance
                        FROM memory_items
                        WHERE revoked_at IS NULL
                          AND embedding IS NOT NULL
                          AND embedding_model = :model
                          AND (valid_from IS NULL OR valid_from <= :now)
                          AND (valid_until IS NULL OR valid_until > :now)
                          AND (
                                scope = 'GLOBAL'
                                OR (scope = 'SUBJECT' AND subject_identity_id = :subject)
                                OR (
                                    scope = 'CONVERSATION'
                                    AND conversation_stable_key = :conversation_key
                                )
                              )
                          {privacy_clause}
                        ORDER BY distance ASC
                        LIMIT :limit
                        """
                    ),
                    {
                        "query": _vector_literal(query_embedding),
                        "model": embedding_model,
                        "subject": subject_identity_id,
                        "conversation_key": conversation_stable_key,
                        "now": now,
                        "limit": limit,
                    },
                )
            ).mappings().all()

            memories = [
                ScoredMemory(
                    id=row["id"],
                    scope=MemoryScope(row["scope"]),
                    privacy=MemoryPrivacy(row["privacy"]),
                    kind=row["kind"],
                    content=row["content"],
                    confidence=row["confidence"],
                    created_at=row["created_at"],
                    distance=float(row["distance"]),
                    supersedes=_uuids_from_json(row["supersedes"]),
                )
                for row in rows
            ]
            superseded = {
                superseded_id
                for memory in memories
                for superseded_id in memory.supersedes
            }
            visible = tuple(
                memory for memory in memories if memory.id not in superseded
            )
            if visible:
                await session.execute(
                    sa.text(
                        "UPDATE memory_items SET last_accessed_at = :now "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"now": now, "ids": [memory.id for memory in visible]},
                )
                await session.commit()
            return visible

    async def revoke_for(
        self,
        *,
        subject_identity_id: str,
        conversation_stable_key: str | None,
        now: datetime,
        reason: str = "user_forget",
    ) -> int:
        """Revoke a subject's memories, optionally plus one conversation's."""

        conversation_clause = (
            "OR (scope = 'CONVERSATION' AND conversation_stable_key = :conversation_key)"
            if conversation_stable_key is not None
            else ""
        )
        async with self.sessions() as session:
            result = await session.execute(
                sa.text(
                    f"""
                    UPDATE memory_items
                    SET revoked_at = :now, revoked_reason = :reason
                    WHERE revoked_at IS NULL
                      AND (
                            (scope = 'SUBJECT' AND subject_identity_id = :subject)
                            {conversation_clause}
                          )
                    """
                ),
                {
                    "now": now,
                    "reason": reason,
                    "subject": subject_identity_id,
                    "conversation_key": conversation_stable_key,
                },
            )
            await session.commit()
            return _rowcount(result)

    async def run_lifecycle(
        self,
        *,
        now: datetime,
        decay_days: int,
        decay_factor: float,
        confidence_floor: float,
        revoked_retention_days: int,
    ) -> LifecycleReport:
        """Expire past-validity items, decay stale ones, purge long-revoked rows."""

        stale_before = now - timedelta(days=decay_days)
        purge_before = now - timedelta(days=revoked_retention_days)
        async with self.sessions() as session:
            expired = await session.execute(
                sa.text(
                    "UPDATE memory_items SET revoked_at = :now, revoked_reason = 'expired' "
                    "WHERE revoked_at IS NULL AND valid_until IS NOT NULL "
                    "AND valid_until <= :now"
                ),
                {"now": now},
            )
            await session.execute(
                sa.text(
                    "UPDATE memory_items SET confidence = confidence * :factor "
                    "WHERE revoked_at IS NULL "
                    "AND COALESCE(last_accessed_at, created_at) <= :stale_before"
                ),
                {"factor": decay_factor, "stale_before": stale_before},
            )
            decayed = await session.execute(
                sa.text(
                    "UPDATE memory_items SET revoked_at = :now, revoked_reason = 'decayed' "
                    "WHERE revoked_at IS NULL AND confidence < :floor"
                ),
                {"now": now, "floor": confidence_floor},
            )
            deleted = await session.execute(
                sa.text(
                    "DELETE FROM memory_items "
                    "WHERE revoked_at IS NOT NULL AND revoked_at <= :purge_before"
                ),
                {"purge_before": purge_before},
            )
            await session.commit()
            return LifecycleReport(
                expired=_rowcount(expired),
                decayed=_rowcount(decayed),
                deleted=_rowcount(deleted),
            )


def _uuid_json(values: tuple[UUID, ...]) -> str:
    return json.dumps([str(value) for value in values])


def _uuids_from_json(value: object) -> tuple[UUID, ...]:
    if not isinstance(value, list):
        return ()
    collected: list[UUID] = []
    for entry in cast(list[object], value):
        try:
            collected.append(UUID(str(entry)))
        except ValueError:
            continue
    return tuple(collected)
