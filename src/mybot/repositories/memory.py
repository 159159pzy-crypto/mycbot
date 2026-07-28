"""Scoped, revocable memory persistence over pgvector."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryMergeDecision,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
)


@dataclass(slots=True, frozen=True)
class ScoredMemory:
    id: UUID
    scope: MemoryScope
    privacy: MemoryPrivacy
    kind: str
    content: str
    confidence: float
    created_at: datetime
    distance: float | None
    supersedes: tuple[UUID, ...]
    vector_rank: int | None = None
    text_rank: int | None = None
    rrf_score: float = 0.0
    relationship_score: float | None = None


@dataclass(slots=True, frozen=True)
class MemoryRecord:
    id: UUID
    scope: MemoryScope
    subject_identity_id: str | None
    conversation_stable_key: str | None
    privacy: MemoryPrivacy
    kind: str
    content: str
    confidence: float
    source_message_ids: tuple[str, ...]
    created_at: datetime
    relationship_score: float | None = None


@dataclass(slots=True, frozen=True)
class MergeApplication:
    operation: MemoryOperation
    memory_id: UUID | None
    previous_memory_id: UUID | None
    applied: bool


@dataclass(slots=True, frozen=True)
class ConsolidationContext:
    conversation: ConversationKey
    subject_identity_id: str
    source_message_ids: tuple[str, ...]
    transcript: tuple[str, ...]
    memories: tuple[MemoryRecord, ...]


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
            await self._insert(
                session,
                item,
                embedding=embedding,
                embedding_model=embedding_model,
            )
            await session.commit()
        return item.id

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        query_text: str,
        embedding_model: str,
        subject_identity_id: str,
        conversation_stable_key: str,
        include_private: bool,
        now: datetime,
        limit: int = 5,
    ) -> tuple[ScoredMemory, ...]:
        """Hybrid vector/trigram recall under one SQL privacy and validity boundary."""

        privacy_clause = (
            "" if include_private else "AND privacy NOT IN ('PRIVATE', 'SENSITIVE')"
        )
        async with self.sessions() as session:
            candidate_limit = max(limit * 4, limit)
            rows = (
                await session.execute(
                    sa.text(
                        f"""
                        WITH eligible AS (
                            SELECT id, scope, privacy, kind, content, confidence,
                                   relationship_score,
                                   created_at, supersedes, embedding, embedding_model,
                                   embedding <=> CAST(:query AS vector) AS distance,
                                   similarity(content, :query_text) AS text_score
                            FROM memory_items
                            WHERE revoked_at IS NULL
                              AND (invalid_at IS NULL OR invalid_at > :now)
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
                        ),
                        vector_hits AS (
                            SELECT id, distance,
                                   row_number() OVER (
                                       ORDER BY distance, created_at DESC, id
                                   ) AS rank
                            FROM eligible
                            WHERE embedding IS NOT NULL AND embedding_model = :model
                            ORDER BY distance, created_at DESC, id
                            LIMIT :candidate_limit
                        ),
                        text_hits AS (
                            SELECT id,
                                   row_number() OVER (
                                       ORDER BY text_score DESC, created_at DESC, id
                                   ) AS rank
                            FROM eligible
                            WHERE :query_text <> '' AND text_score > 0
                            ORDER BY text_score DESC, created_at DESC, id
                            LIMIT :candidate_limit
                        ),
                        fused AS (
                            SELECT COALESCE(vector_hits.id, text_hits.id) AS id,
                                   vector_hits.distance,
                                   vector_hits.rank AS vector_rank,
                                   text_hits.rank AS text_rank,
                                   COALESCE(1.0 / (60 + vector_hits.rank), 0.0)
                                   + COALESCE(1.0 / (60 + text_hits.rank), 0.0)
                                   AS rrf_score
                            FROM vector_hits
                            FULL OUTER JOIN text_hits ON text_hits.id = vector_hits.id
                        )
                        SELECT eligible.id, eligible.scope, eligible.privacy,
                               eligible.kind, eligible.content, eligible.confidence,
                               eligible.relationship_score,
                               eligible.created_at, eligible.supersedes,
                               fused.distance, fused.vector_rank, fused.text_rank,
                               fused.rrf_score
                        FROM fused
                        JOIN eligible ON eligible.id = fused.id
                        ORDER BY fused.rrf_score DESC,
                                 COALESCE(fused.vector_rank, 2147483647),
                                 COALESCE(fused.text_rank, 2147483647),
                                 eligible.created_at DESC,
                                 eligible.id
                        LIMIT :limit
                        """
                    ),
                    {
                        "query": _vector_literal(query_embedding),
                        "query_text": query_text.strip(),
                        "model": embedding_model,
                        "subject": subject_identity_id,
                        "conversation_key": conversation_stable_key,
                        "now": now,
                        "limit": limit,
                        "candidate_limit": candidate_limit,
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
                    relationship_score=(
                        float(row["relationship_score"])
                        if row["relationship_score"] is not None
                        else None
                    ),
                    created_at=row["created_at"],
                    distance=(
                        float(row["distance"]) if row["distance"] is not None else None
                    ),
                    supersedes=_uuids_from_json(row["supersedes"]),
                    vector_rank=(
                        int(row["vector_rank"])
                        if row["vector_rank"] is not None
                        else None
                    ),
                    text_rank=(
                        int(row["text_rank"])
                        if row["text_rank"] is not None
                        else None
                    ),
                    rrf_score=float(row["rrf_score"]),
                )
                for row in rows
            ]
            visible = tuple(memories)
            if visible:
                await session.execute(
                    sa.text(
                        "UPDATE memory_items SET last_accessed_at = :now "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"now": now, "ids": [memory.id for memory in visible]},
                )
            await session.execute(
                sa.text(
                    """
                    INSERT INTO memory_recall_audit (
                        id, query_hash, subject_identity_id, conversation_stable_key,
                        vector_ids, text_ids, selected_ids
                    ) VALUES (
                        :id, :query_hash, :subject, :conversation_key,
                        CAST(:vector_ids AS jsonb), CAST(:text_ids AS jsonb),
                        CAST(:selected_ids AS jsonb)
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "query_hash": sha256(query_text.strip().encode("utf-8")).hexdigest(),
                    "subject": subject_identity_id,
                    "conversation_key": conversation_stable_key,
                    "vector_ids": _ranked_ids(rows, "vector_rank"),
                    "text_ids": _ranked_ids(rows, "text_rank"),
                    "selected_ids": json.dumps([str(memory.id) for memory in visible]),
                },
            )
            await session.commit()
            return visible

    async def similar_for_merge(
        self,
        item: MemoryItem,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        now: datetime,
        limit: int = 5,
    ) -> tuple[MemoryRecord, ...]:
        conversation_key = item.conversation.stable_key if item.conversation else None
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               privacy, kind, content, confidence, relationship_score,
                               source_message_ids,
                               created_at
                        FROM memory_items
                        WHERE revoked_at IS NULL
                          AND (invalid_at IS NULL OR invalid_at > :now)
                          AND (valid_from IS NULL OR valid_from <= :now)
                          AND (valid_until IS NULL OR valid_until > :now)
                          AND scope = :scope
                          AND privacy = :privacy
                          AND subject_identity_id IS NOT DISTINCT FROM :subject
                          AND conversation_stable_key IS NOT DISTINCT FROM :conversation_key
                          AND (
                              (embedding IS NOT NULL AND embedding_model = :model)
                              OR similarity(content, :query_text) > 0
                          )
                        ORDER BY (
                            CASE
                                WHEN embedding IS NOT NULL AND embedding_model = :model
                                THEN (1 - (embedding <=> CAST(:query AS vector))) * 0.7
                                ELSE 0
                            END
                            + similarity(content, :query_text) * 0.3
                        ) DESC, created_at DESC, id
                        LIMIT :limit
                        """
                    ),
                    {
                        "now": now,
                        "scope": item.scope.value,
                        "privacy": item.privacy.value,
                        "subject": item.subject_identity_id,
                        "conversation_key": conversation_key,
                        "model": embedding_model,
                        "query": _vector_literal(query_embedding),
                        "query_text": item.content,
                        "limit": limit,
                    },
                )
            ).mappings().all()
            return tuple(_memory_record(row) for row in rows)

    async def apply_merge(
        self,
        candidate: MemoryItem,
        decision: MemoryMergeDecision,
        *,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
        source: str,
        now: datetime,
    ) -> MergeApplication:
        async with self.sessions() as session:
            target = None
            if decision.target_id is not None:
                target = (
                    await session.execute(
                        sa.text(
                            """
                            SELECT id, scope, subject_identity_id,
                                   conversation_stable_key, privacy
                            FROM memory_items
                            WHERE id = :id AND revoked_at IS NULL
                              AND (invalid_at IS NULL OR invalid_at > :now)
                            FOR UPDATE
                            """
                        ),
                        {"id": decision.target_id, "now": now},
                    )
                ).mappings().one_or_none()
                if target is None or not _same_boundary(target, candidate):
                    await self._audit(
                        session,
                        operation=MemoryOperation.NOOP,
                        source=source,
                        candidate=candidate,
                        previous_id=decision.target_id,
                        detail={"reason": "target_not_active_or_out_of_scope"},
                    )
                    await session.commit()
                    return MergeApplication(
                        operation=MemoryOperation.NOOP,
                        memory_id=None,
                        previous_memory_id=decision.target_id,
                        applied=False,
                    )

            operation = decision.operation
            memory_id: UUID | None = None
            previous_id = decision.target_id
            if operation in {MemoryOperation.ADD, MemoryOperation.UPDATE}:
                updates: dict[str, object] = {
                    "content": decision.content or candidate.content,
                }
                if decision.kind is not None:
                    updates["kind"] = decision.kind
                if decision.confidence is not None:
                    updates["confidence"] = decision.confidence
                if operation is MemoryOperation.UPDATE and previous_id is not None:
                    updates["id"] = uuid4()
                    updates["supersedes"] = tuple(
                        dict.fromkeys((*candidate.supersedes, previous_id))
                    )
                successor = candidate.model_copy(update=updates)
                await self._insert(
                    session,
                    successor,
                    embedding=embedding,
                    embedding_model=embedding_model,
                )
                memory_id = successor.id
                if operation is MemoryOperation.UPDATE and previous_id is not None:
                    await session.execute(
                        sa.text(
                            "UPDATE memory_items SET invalid_at = :now, "
                            "invalidated_by = :successor WHERE id = :id"
                        ),
                        {"now": now, "successor": successor.id, "id": previous_id},
                    )
            elif operation is MemoryOperation.DELETE and previous_id is not None:
                await session.execute(
                    sa.text(
                        "UPDATE memory_items SET invalid_at = :now, invalidated_by = NULL "
                        "WHERE id = :id"
                    ),
                    {"now": now, "id": previous_id},
                )
            await self._audit(
                session,
                operation=operation,
                source=source,
                candidate=candidate,
                memory_id=memory_id,
                previous_id=previous_id,
                detail={"applied": operation is not MemoryOperation.NOOP},
            )
            await session.commit()
            return MergeApplication(
                operation=operation,
                memory_id=memory_id,
                previous_memory_id=previous_id,
                applied=operation is not MemoryOperation.NOOP,
            )

    async def _insert(
        self,
        session: AsyncSession,
        item: MemoryItem,
        *,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
    ) -> None:
        await session.execute(
            sa.text(
                """
                INSERT INTO memory_items (
                    id, scope, subject_identity_id, conversation_stable_key,
                    kind, content, source_message_ids, confidence,
                    relationship_score, privacy,
                    valid_from, valid_until, conflicts_with, supersedes,
                    invalid_at, invalidated_by, revoked_at, revoked_reason,
                    embedding, embedding_model
                ) VALUES (
                    :id, :scope, :subject, :conversation_key,
                    :kind, :content, CAST(:sources AS jsonb), :confidence,
                    :relationship_score, :privacy,
                    :valid_from, :valid_until, CAST(:conflicts AS jsonb),
                    CAST(:supersedes AS jsonb), :invalid_at, :invalidated_by,
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
                "relationship_score": item.relationship_score,
                "privacy": item.privacy.value,
                "valid_from": item.valid_from,
                "valid_until": item.valid_until,
                "conflicts": _uuid_json(item.conflicts_with),
                "supersedes": _uuid_json(item.supersedes),
                "invalid_at": item.invalid_at,
                "invalidated_by": item.invalidated_by,
                "revoked_at": item.revoked_at,
                "revoked_reason": item.revoked_reason,
                "embedding": (
                    _vector_literal(embedding) if embedding is not None else None
                ),
                "embedding_model": embedding_model,
            },
        )

    async def _audit(
        self,
        session: AsyncSession,
        *,
        operation: MemoryOperation,
        source: str,
        candidate: MemoryItem,
        memory_id: UUID | None = None,
        previous_id: UUID | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        await session.execute(
            sa.text(
                """
                INSERT INTO memory_operation_audit (
                    id, operation, source, memory_id, previous_memory_id,
                    conversation_stable_key, subject_identity_id, detail
                ) VALUES (
                    :id, :operation, :source, :memory_id, :previous_id,
                    :conversation_key, :subject, CAST(:detail AS jsonb)
                )
                """
            ),
            {
                "id": uuid4(),
                "operation": operation.value,
                "source": source,
                "memory_id": memory_id,
                "previous_id": previous_id,
                "conversation_key": (
                    candidate.conversation.stable_key
                    if candidate.conversation is not None
                    else None
                ),
                "subject": candidate.subject_identity_id,
                "detail": json.dumps(dict(detail or {}), default=str),
            },
        )

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

    async def active_expressions(
        self, conversation_stable_key: str, *, now: datetime, limit: int = 3
    ) -> tuple[MemoryRecord, ...]:
        """Return active shared expression examples from exactly one conversation."""

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               privacy, kind, content, confidence, relationship_score,
                               source_message_ids, created_at
                        FROM memory_items
                        WHERE scope = 'CONVERSATION'
                          AND conversation_stable_key = :stable_key
                          AND privacy = 'SHARED'
                          AND kind = 'EXPRESSION'
                          AND revoked_at IS NULL
                          AND (invalid_at IS NULL OR invalid_at > :now)
                          AND (valid_from IS NULL OR valid_from <= :now)
                          AND (valid_until IS NULL OR valid_until > :now)
                        ORDER BY confidence DESC, created_at DESC, id
                        LIMIT :limit
                        """
                    ),
                    {"stable_key": conversation_stable_key, "now": now, "limit": limit},
                )
            ).mappings().all()
        return tuple(_memory_record(row) for row in rows)

    async def relationship_for(
        self, subject_identity_id: str, *, now: datetime
    ) -> MemoryRecord | None:
        """Return the current relationship version for one subject."""

        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               privacy, kind, content, confidence, relationship_score,
                               source_message_ids, created_at
                        FROM memory_items
                        WHERE scope = 'SUBJECT'
                          AND subject_identity_id = :subject
                          AND kind = 'RELATIONSHIP'
                          AND revoked_at IS NULL
                          AND (invalid_at IS NULL OR invalid_at > :now)
                          AND (valid_from IS NULL OR valid_from <= :now)
                          AND (valid_until IS NULL OR valid_until > :now)
                        ORDER BY created_at DESC, id
                        LIMIT 1
                        """
                    ),
                    {"subject": subject_identity_id, "now": now},
                )
            ).mappings().one_or_none()
        return _memory_record(row) if row is not None else None

    async def upsert_relationship(
        self,
        item: MemoryItem,
        *,
        source: str,
        now: datetime,
    ) -> MergeApplication:
        """Append a relationship successor and invalidate its previous active version."""

        if (
            item.scope is not MemoryScope.SUBJECT
            or item.kind.upper() != "RELATIONSHIP"
            or item.relationship_score is None
        ):
            raise ValueError("relationship item must be SUBJECT/RELATIONSHIP with a score")
        async with self.sessions() as session, session.begin():
            previous = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id
                        FROM memory_items
                        WHERE scope = 'SUBJECT'
                          AND subject_identity_id = :subject
                          AND kind = 'RELATIONSHIP'
                          AND revoked_at IS NULL
                          AND (invalid_at IS NULL OR invalid_at > :now)
                        ORDER BY created_at DESC, id
                        LIMIT 1
                        FOR UPDATE
                        """
                    ),
                    {"subject": item.subject_identity_id, "now": now},
                )
            ).mappings().one_or_none()
            previous_id = cast(UUID | None, previous["id"] if previous else None)
            successor = item
            operation = MemoryOperation.ADD
            if previous_id is not None:
                operation = MemoryOperation.UPDATE
                successor = item.model_copy(
                    update={
                        "id": uuid4(),
                        "supersedes": tuple(
                            dict.fromkeys((*item.supersedes, previous_id))
                        ),
                    }
                )
            await self._insert(
                session,
                successor,
                embedding=None,
                embedding_model=None,
            )
            if previous_id is not None:
                await session.execute(
                    sa.text(
                        "UPDATE memory_items SET invalid_at = :now, "
                        "invalidated_by = :successor WHERE id = :previous"
                    ),
                    {
                        "now": now,
                        "successor": successor.id,
                        "previous": previous_id,
                    },
                )
            await self._audit(
                session,
                operation=operation,
                source=source,
                candidate=successor,
                memory_id=successor.id,
                previous_id=previous_id,
                detail={"relationship_score": successor.relationship_score},
            )
        return MergeApplication(
            operation=operation,
            memory_id=successor.id,
            previous_memory_id=previous_id,
            applied=True,
        )

    async def recent_consolidation_contexts(
        self,
        *,
        since: datetime,
        now: datetime,
        conversation_limit: int,
        message_limit: int,
        memory_limit: int,
    ) -> tuple[ConsolidationContext, ...]:
        """Load bounded recent dialogue plus active memories without crossing privacy."""

        async with self.sessions() as session:
            conversations = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, connection_id, chat_kind, chat_id, thread_id,
                               stable_key, sender_identity_id
                        FROM (
                            SELECT DISTINCT ON (c.id)
                                   c.id, c.connection_id, c.chat_kind, c.chat_id,
                                   c.thread_id, c.stable_key, m.sender_identity_id,
                                   m.occurred_at
                            FROM conversations c
                            JOIN messages m ON m.conversation_id = c.id
                            WHERE c.ephemeral = false
                              AND m.direction = 'inbound'
                              AND m.occurred_at >= :since
                            ORDER BY c.id, m.occurred_at DESC
                        ) recent
                        ORDER BY occurred_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"since": since, "limit": conversation_limit},
                )
            ).mappings().all()
            contexts: list[ConsolidationContext] = []
            for conversation_row in conversations:
                message_rows = (
                    await session.execute(
                        sa.text(
                            """
                            SELECT id, direction, sender_identity_id, segments
                            FROM messages
                            WHERE conversation_id = :conversation_id
                              AND occurred_at >= :since
                            ORDER BY occurred_at DESC
                            LIMIT :limit
                            """
                        ),
                        {
                            "conversation_id": conversation_row["id"],
                            "since": since,
                            "limit": message_limit,
                        },
                    )
                ).mappings().all()
                chat_kind = ChatKind(str(conversation_row["chat_kind"]))
                privacy_clause = (
                    "AND privacy <> 'SENSITIVE'"
                    if chat_kind is ChatKind.DIRECT
                    else "AND privacy NOT IN ('PRIVATE', 'SENSITIVE')"
                )
                memory_rows = (
                    await session.execute(
                        sa.text(
                            f"""
                            SELECT id, scope, subject_identity_id,
                                   conversation_stable_key, privacy, kind, content,
                                   confidence, relationship_score,
                                   source_message_ids, created_at
                            FROM memory_items
                            WHERE revoked_at IS NULL
                              AND (invalid_at IS NULL OR invalid_at > :now)
                              AND (valid_from IS NULL OR valid_from <= :now)
                              AND (valid_until IS NULL OR valid_until > :now)
                              AND (
                                  scope = 'GLOBAL'
                                  OR (
                                      scope = 'SUBJECT'
                                      AND subject_identity_id = :subject
                                  )
                                  OR (
                                      scope = 'CONVERSATION'
                                      AND conversation_stable_key = :stable_key
                                  )
                              )
                              {privacy_clause}
                            ORDER BY confidence DESC, created_at DESC
                            LIMIT :limit
                            """
                        ),
                        {
                            "now": now,
                            "subject": conversation_row["sender_identity_id"],
                            "stable_key": conversation_row["stable_key"],
                            "limit": memory_limit,
                        },
                    )
                ).mappings().all()
                rendered_messages = [
                    (
                        str(row["id"]),
                        f"{row['direction']}:{row['sender_identity_id']}: "
                        f"{_text_from_segments(row['segments'])}",
                    )
                    for row in reversed(message_rows)
                    if _text_from_segments(row["segments"])
                ]
                if not rendered_messages:
                    continue
                contexts.append(
                    ConsolidationContext(
                        conversation=ConversationKey(
                            connection_id=str(conversation_row["connection_id"]),
                            chat_kind=chat_kind,
                            chat_id=str(conversation_row["chat_id"]),
                            thread_id=cast(str | None, conversation_row["thread_id"]),
                        ),
                        subject_identity_id=str(
                            conversation_row["sender_identity_id"]
                        ),
                        source_message_ids=tuple(
                            message_id for message_id, _ in rendered_messages
                        ),
                        transcript=tuple(text for _, text in rendered_messages),
                        memories=tuple(_memory_record(row) for row in memory_rows),
                    )
                )
            return tuple(contexts)

    async def run_lifecycle(
        self,
        *,
        now: datetime,
        decay_days: int,
        decay_factor: float,
        confidence_floor: float,
        revoked_retention_days: int,
    ) -> LifecycleReport:
        """Invalidate expired/decayed items; purge only privacy-revoked rows."""

        stale_before = now - timedelta(days=decay_days)
        purge_before = now - timedelta(days=revoked_retention_days)
        async with self.sessions() as session:
            expired = await session.execute(
                sa.text(
                    "UPDATE memory_items SET invalid_at = valid_until "
                    "WHERE revoked_at IS NULL AND invalid_at IS NULL "
                    "AND valid_until IS NOT NULL "
                    "AND valid_until <= :now"
                ),
                {"now": now},
            )
            await session.execute(
                sa.text(
                    "UPDATE memory_items SET confidence = confidence * :factor, "
                    "relationship_score = CASE WHEN relationship_score IS NULL THEN NULL "
                    "ELSE GREATEST(0, relationship_score * :factor) END "
                    "WHERE revoked_at IS NULL AND invalid_at IS NULL "
                    "AND COALESCE(last_accessed_at, created_at) <= :stale_before"
                ),
                {"factor": decay_factor, "stale_before": stale_before},
            )
            decayed = await session.execute(
                sa.text(
                    "UPDATE memory_items SET invalid_at = :now "
                    "WHERE revoked_at IS NULL AND invalid_at IS NULL "
                    "AND confidence < :floor"
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


def _ranked_ids(rows: Sequence[RowMapping], field: str) -> str:
    ranked = sorted(
        (
            (int(cast(int, row[field])), str(row["id"]))
            for row in rows
            if row.get(field) is not None
        ),
        key=lambda entry: entry[0],
    )
    return json.dumps([memory_id for _, memory_id in ranked])


def _same_boundary(row: RowMapping, candidate: MemoryItem) -> bool:
    conversation_key = (
        candidate.conversation.stable_key if candidate.conversation is not None else None
    )
    return (
        row["scope"] == candidate.scope.value
        and row["privacy"] == candidate.privacy.value
        and row["subject_identity_id"] == candidate.subject_identity_id
        and row["conversation_stable_key"] == conversation_key
    )


def _memory_record(row: RowMapping) -> MemoryRecord:
    raw_sources = row["source_message_ids"]
    sources = (
        tuple(str(value) for value in cast(list[object], raw_sources))
        if isinstance(raw_sources, list)
        else ()
    )
    return MemoryRecord(
        id=cast(UUID, row["id"]),
        scope=MemoryScope(str(row["scope"])),
        subject_identity_id=cast(str | None, row["subject_identity_id"]),
        conversation_stable_key=cast(str | None, row["conversation_stable_key"]),
        privacy=MemoryPrivacy(str(row["privacy"])),
        kind=str(row["kind"]),
        content=str(row["content"]),
        confidence=float(cast(float, row["confidence"])),
        relationship_score=(
            float(row["relationship_score"])
            if row.get("relationship_score") is not None
            else None
        ),
        source_message_ids=sources,
        created_at=cast(datetime, row["created_at"]),
    )


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


def _text_from_segments(value: object) -> str:
    if not isinstance(value, list):
        return ""
    texts: list[str] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        segment = cast(dict[str, object], raw)
        if segment.get("type") != "text":
            continue
        text = segment.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n".join(texts)
