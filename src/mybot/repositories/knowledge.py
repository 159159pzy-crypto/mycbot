"""PostgreSQL persistence for hierarchical knowledge and annotation replies."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import (
    AnnotationReply,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    KnowledgeScope,
    KnowledgeSearchHit,
    KnowledgeSourceType,
)
from mybot.engine.knowledge import (
    IngestClaim,
    IngestClaimStatus,
    IngestSource,
    PreparedParent,
)


def _vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


@dataclass(slots=True)
class KnowledgeRepository:
    sessions: async_sessionmaker[AsyncSession]
    ingestion_lease_seconds: int = 300

    async def create_document(
        self, document: KnowledgeDocument, *, content: bytes
    ) -> tuple[KnowledgeDocument, bool]:
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    sa.text(
                        """
                        INSERT INTO kb_document (
                            id, title, source_type, scope, conversation_id, status,
                            content_hash, original_filename, source_content, generation
                        ) VALUES (
                            :id, :title, :source_type, :scope, :conversation_id, :status,
                            :content_hash, :original_filename, :source_content, :generation
                        )
                        ON CONFLICT (content_hash, scope, conversation_id)
                        DO NOTHING
                        RETURNING id, title, source_type, scope, conversation_id,
                                  status, content_hash, original_filename, generation
                        """
                    ),
                    {
                        "id": document.id,
                        "title": document.title,
                        "source_type": document.source_type.value,
                        "scope": document.scope.value,
                        "conversation_id": document.conversation_id,
                        "status": document.status.value,
                        "content_hash": document.content_hash,
                        "original_filename": document.original_filename,
                        "source_content": content,
                        "generation": document.generation,
                    },
                )
            ).mappings().one_or_none()
            created = row is not None
            if row is None:
                row = (
                    await session.execute(
                        sa.text(
                            """
                            SELECT id, title, source_type, scope, conversation_id,
                                   status, content_hash, original_filename, generation
                            FROM kb_document
                            WHERE content_hash = :content_hash AND scope = :scope
                              AND conversation_id IS NOT DISTINCT FROM :conversation_id
                            """
                        ),
                        {
                            "content_hash": document.content_hash,
                            "scope": document.scope.value,
                            "conversation_id": document.conversation_id,
                        },
                    )
                ).mappings().one()
            if created or row["status"] in {
                KnowledgeDocumentStatus.QUEUED.value,
                KnowledgeDocumentStatus.FAILED.value,
            }:
                await session.execute(
                    sa.text(
                        """
                        INSERT INTO kb_ingest_outbox (document_id, generation)
                        VALUES (:document_id, :generation)
                        ON CONFLICT (document_id, generation) DO UPDATE
                        SET published_at = CASE
                                WHEN :reset_published THEN NULL
                                ELSE kb_ingest_outbox.published_at
                            END,
                            error_code = CASE
                                WHEN :reset_published THEN NULL
                                ELSE kb_ingest_outbox.error_code
                            END
                        """
                    ),
                    {
                        "document_id": row["id"],
                        "generation": int(row["generation"]),
                        "reset_published": (
                            row["status"] == KnowledgeDocumentStatus.FAILED.value
                        ),
                    },
                )
            return self._document(row), created

    async def pending_ingest_tasks(
        self, *, limit: int = 50, document_id: UUID | None = None
    ) -> tuple[tuple[UUID, int], ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT document_id, generation
                        FROM kb_ingest_outbox
                        WHERE published_at IS NULL
                          AND (:document_id IS NULL OR document_id = :document_id)
                        ORDER BY created_at
                        LIMIT :limit
                        """
                    ),
                    {"document_id": document_id, "limit": max(1, min(limit, 500))},
                )
            ).all()
        return tuple((row[0], int(row[1])) for row in rows)

    async def mark_ingest_task_published(self, document_id: UUID, generation: int) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE kb_ingest_outbox
                    SET published_at = CURRENT_TIMESTAMP, attempts = attempts + 1,
                        error_code = NULL
                    WHERE document_id = :document_id AND generation = :generation
                    """
                ),
                {"document_id": document_id, "generation": generation},
            )

    async def mark_ingest_task_failed(
        self, document_id: UUID, generation: int, *, error_code: str
    ) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE kb_ingest_outbox
                    SET attempts = attempts + 1, error_code = :error_code
                    WHERE document_id = :document_id AND generation = :generation
                      AND published_at IS NULL
                    """
                ),
                {
                    "document_id": document_id,
                    "generation": generation,
                    "error_code": error_code[:200],
                },
            )

    async def list_documents(self, *, limit: int = 100) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT d.id, d.title, d.source_type, d.scope, d.conversation_id,
                               c.stable_key AS conversation_stable_key, d.status,
                               d.content_hash, d.original_filename, d.generation,
                               d.embedding_model, d.error_code, d.created_at, d.updated_at,
                               count(ch.id) FILTER (WHERE ch.level = 'CHILD') AS child_count
                        FROM kb_document d
                        LEFT JOIN conversations c ON c.id = d.conversation_id
                        LEFT JOIN kb_chunk ch ON ch.document_id = d.id
                        GROUP BY d.id, c.stable_key
                        ORDER BY d.updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": max(1, min(limit, 500))},
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "title": row["title"],
                    "source_type": row["source_type"],
                    "scope": row["scope"],
                    "conversation_id": str(row["conversation_id"])
                    if row["conversation_id"]
                    else None,
                    "conversation_stable_key": row["conversation_stable_key"],
                    "status": row["status"],
                    "content_hash": row["content_hash"],
                    "original_filename": row["original_filename"],
                    "generation": int(row["generation"]),
                    "embedding_model": row["embedding_model"],
                    "error_code": row["error_code"],
                    "child_count": int(row["child_count"] or 0),
                    "created_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"]),
                }
                for row in rows
            ]

    async def delete_document(self, document_id: UUID) -> bool:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                sa.text("DELETE FROM kb_document WHERE id = :id"), {"id": document_id}
            )
            return bool(getattr(result, "rowcount", 0))

    async def requeue_document(self, document_id: UUID) -> KnowledgeDocument | None:
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    sa.text(
                        """
                        UPDATE kb_document
                        SET generation = generation + 1, status = 'QUEUED', error_code = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                        RETURNING id, title, source_type, scope, conversation_id, status,
                                  content_hash, original_filename, generation
                        """
                    ),
                    {"id": document_id},
                )
            ).mappings().one_or_none()
            if row is not None:
                await session.execute(
                    sa.text(
                        """
                        INSERT INTO kb_ingest_outbox (document_id, generation)
                        VALUES (:document_id, :generation)
                        ON CONFLICT (document_id, generation) DO NOTHING
                        """
                    ),
                    {"document_id": row["id"], "generation": int(row["generation"])},
                )
            return self._document(row) if row else None

    async def claim_ingestion(
        self, document_id: UUID, generation: int
    ) -> IngestClaim:
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    sa.text(
                        """
                        UPDATE kb_document
                        SET status = 'PROCESSING', error_code = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id AND generation = :generation
                          AND (
                            status IN ('QUEUED', 'FAILED') OR
                            (status = 'PROCESSING' AND
                             updated_at < CURRENT_TIMESTAMP -
                                make_interval(secs => :lease_seconds))
                          )
                        RETURNING id, generation, source_type, source_content
                        """
                    ),
                    {
                        "id": document_id,
                        "generation": generation,
                        "lease_seconds": self.ingestion_lease_seconds,
                    },
                )
            ).mappings().one_or_none()
            if row is None:
                state = (
                    await session.execute(
                        sa.text(
                            "SELECT generation, status FROM kb_document WHERE id = :id"
                        ),
                        {"id": document_id},
                    )
                ).mappings().one_or_none()
                if (
                    state is not None
                    and int(state["generation"]) == generation
                    and state["status"] == KnowledgeDocumentStatus.PROCESSING.value
                ):
                    return IngestClaim(IngestClaimStatus.BUSY)
                return IngestClaim(IngestClaimStatus.TERMINAL)
            return IngestClaim(
                IngestClaimStatus.ACQUIRED,
                IngestSource(
                    document_id=row["id"],
                    generation=int(row["generation"]),
                    source_type=KnowledgeSourceType(row["source_type"]),
                    content=bytes(row["source_content"]),
                ),
            )

    async def complete_ingestion(
        self,
        source: IngestSource,
        *,
        raw_text: str,
        parents: Sequence[PreparedParent],
        child_embeddings: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> bool:
        async with self.sessions() as session, session.begin():
            locked = (
                await session.execute(
                    sa.text(
                        """
                        SELECT generation, status FROM kb_document
                        WHERE id = :id FOR UPDATE
                        """
                    ),
                    {"id": source.document_id},
                )
            ).mappings().one_or_none()
            if (
                locked is None
                or int(locked["generation"]) != source.generation
                or locked["status"] != KnowledgeDocumentStatus.PROCESSING.value
            ):
                return False
            await session.execute(
                sa.text("DELETE FROM kb_chunk WHERE document_id = :id"),
                {"id": source.document_id},
            )
            vector_index = 0
            for parent in parents:
                parent_id = uuid4()
                await session.execute(
                    sa.text(
                        """
                        INSERT INTO kb_chunk (
                            id, document_id, parent_id, chunk_index, level,
                            content, token_count, metadata
                        ) VALUES (
                            :id, :document_id, NULL, :chunk_index, 'PARENT',
                            :content, :token_count, CAST(:metadata AS jsonb)
                        )
                        """
                    ),
                    {
                        "id": parent_id,
                        "document_id": source.document_id,
                        "chunk_index": parent.index,
                        "content": parent.content,
                        "token_count": parent.token_count,
                        "metadata": json.dumps({"generation": source.generation}),
                    },
                )
                for child in parent.children:
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO kb_chunk (
                                id, document_id, parent_id, chunk_index, level,
                                content, token_count, embedding, embedding_model, metadata
                            ) VALUES (
                                :id, :document_id, :parent_id, :chunk_index, 'CHILD',
                                :content, :token_count, CAST(:embedding AS vector),
                                :embedding_model, CAST(:metadata AS jsonb)
                            )
                            """
                        ),
                        {
                            "id": uuid4(),
                            "document_id": source.document_id,
                            "parent_id": parent_id,
                            "chunk_index": parent.index * 100_000 + child.index,
                            "content": child.content,
                            "token_count": child.token_count,
                            "embedding": _vector_literal(child_embeddings[vector_index]),
                            "embedding_model": embedding_model,
                            "metadata": json.dumps(
                                {
                                    "generation": source.generation,
                                    "parent_index": parent.index,
                                    "child_index": child.index,
                                }
                            ),
                        },
                    )
                    vector_index += 1
            await session.execute(
                sa.text(
                    """
                    UPDATE kb_document
                    SET status = 'READY', raw_text = :raw_text,
                        embedding_model = :embedding_model, error_code = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND generation = :generation
                    """
                ),
                {
                    "id": source.document_id,
                    "generation": source.generation,
                    "raw_text": raw_text,
                    "embedding_model": embedding_model,
                },
            )
            return True

    async def fail_ingestion(
        self, document_id: UUID, generation: int, *, error_code: str
    ) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE kb_document
                    SET status = 'FAILED', error_code = :error_code,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND generation = :generation
                    """
                ),
                {"id": document_id, "generation": generation, "error_code": error_code[:200]},
            )

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        conversation_stable_key: str | None,
        include_global: bool,
        allow_conversation: bool,
        top_k: int,
        threshold: float,
    ) -> tuple[KnowledgeSearchHit, ...]:
        vector = _vector_literal(query_embedding)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT child.id AS child_id, parent.id AS parent_id,
                               d.id AS document_id, d.title AS document_title,
                               child.content AS child_content,
                               parent.content AS parent_content, d.scope,
                               1 - (child.embedding <=> CAST(:embedding AS vector)) AS score
                        FROM kb_chunk child
                        JOIN kb_chunk parent ON parent.id = child.parent_id
                        JOIN kb_document d ON d.id = child.document_id
                        WHERE child.level = 'CHILD' AND d.status = 'READY'
                          AND child.embedding IS NOT NULL
                          AND child.embedding_model = :embedding_model
                          AND (
                            (:include_global AND d.scope = 'GLOBAL') OR
                            (:allow_conversation AND d.scope = 'CONVERSATION' AND
                             d.conversation_id = (
                               SELECT id FROM conversations WHERE stable_key = :stable_key
                             ))
                          )
                          AND 1 - (child.embedding <=> CAST(:embedding AS vector)) >= :threshold
                        ORDER BY child.embedding <=> CAST(:embedding AS vector), child.id
                        LIMIT :limit
                        """
                    ),
                    {
                        "embedding": vector,
                        "embedding_model": embedding_model,
                        "include_global": include_global,
                        "allow_conversation": allow_conversation,
                        "stable_key": conversation_stable_key,
                        "threshold": threshold,
                        "limit": top_k,
                    },
                )
            ).mappings().all()
            return tuple(
                KnowledgeSearchHit(
                    child_id=row["child_id"],
                    parent_id=row["parent_id"],
                    document_id=row["document_id"],
                    document_title=row["document_title"],
                    child_content=row["child_content"],
                    parent_content=row["parent_content"],
                    score=max(0.0, min(1.0, float(row["score"]))),
                    scope=KnowledgeScope(row["scope"]),
                )
                for row in rows
            )

    async def create_annotation(
        self,
        annotation: AnnotationReply,
        *,
        embedding: Sequence[float],
        embedding_model: str,
    ) -> AnnotationReply:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO annotation (
                        id, scope, conversation_id, question, answer,
                        embedding, embedding_model, threshold, enabled, source_message_id
                    ) VALUES (
                        :id, :scope, :conversation_id, :question, :answer,
                        CAST(:embedding AS vector), :embedding_model,
                        :threshold, :enabled, :source_message_id
                    )
                    """
                ),
                {
                    "id": annotation.id,
                    "scope": annotation.scope.value,
                    "conversation_id": annotation.conversation_id,
                    "question": annotation.question,
                    "answer": annotation.answer,
                    "embedding": _vector_literal(embedding),
                    "embedding_model": embedding_model,
                    "threshold": annotation.threshold,
                    "enabled": annotation.enabled,
                    "source_message_id": annotation.source_message_id,
                },
            )
        return annotation

    async def annotation_from_message(
        self,
        *,
        conversation_id: UUID,
        message_id: UUID,
        question: str | None,
        threshold: float,
        embedding: Sequence[float],
        embedding_model: str,
    ) -> AnnotationReply | None:
        pair = await self.message_annotation_pair(conversation_id, message_id)
        if pair is None:
            return None
        previous_question, answer = pair
        resolved_question = (question or previous_question).strip()
        if not resolved_question:
            return None
        annotation = AnnotationReply(
            scope=KnowledgeScope.CONVERSATION,
            conversation_id=conversation_id,
            question=resolved_question,
            answer=answer,
            threshold=threshold,
            source_message_id=message_id,
        )
        return await self.create_annotation(
            annotation, embedding=embedding, embedding_model=embedding_model
        )

    async def message_annotation_pair(
        self, conversation_id: UUID, message_id: UUID
    ) -> tuple[str, str] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        WITH target AS (
                            SELECT id, conversation_id, occurred_at, segments
                            FROM messages
                            WHERE id = :message_id AND conversation_id = :conversation_id
                              AND direction = 'outbound'
                        ), previous AS (
                            SELECT m.segments
                            FROM messages m, target t
                            WHERE m.conversation_id = t.conversation_id
                              AND m.direction = 'inbound'
                              AND m.occurred_at <= t.occurred_at
                            ORDER BY m.occurred_at DESC
                            LIMIT 1
                        )
                        SELECT
                            (SELECT string_agg(part->>'text', E'\n')
                             FROM target, jsonb_array_elements(target.segments) part
                             WHERE part->>'type' = 'text') AS answer,
                            (SELECT string_agg(part->>'text', E'\n')
                             FROM previous, jsonb_array_elements(previous.segments) part
                             WHERE part->>'type' = 'text') AS previous_question
                        """
                    ),
                    {"message_id": message_id, "conversation_id": conversation_id},
                )
            ).mappings().one_or_none()
        if row is None or not row["answer"] or not row["previous_question"]:
            return None
        return str(row["previous_question"]).strip(), str(row["answer"]).strip()

    async def list_annotations(self, *, limit: int = 200) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT a.id, a.scope, a.conversation_id, c.stable_key,
                               a.question, a.answer, a.embedding_model, a.threshold,
                               a.enabled, a.source_message_id, a.hit_count,
                               a.last_hit_at, a.created_at, a.updated_at
                        FROM annotation a
                        LEFT JOIN conversations c ON c.id = a.conversation_id
                        ORDER BY a.updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": max(1, min(limit, 500))},
                )
            ).mappings().all()
            return [
                {
                    "id": str(row["id"]),
                    "scope": row["scope"],
                    "conversation_id": str(row["conversation_id"])
                    if row["conversation_id"]
                    else None,
                    "conversation_stable_key": row["stable_key"],
                    "question": row["question"],
                    "answer": row["answer"],
                    "embedding_model": row["embedding_model"],
                    "threshold": float(row["threshold"]),
                    "enabled": bool(row["enabled"]),
                    "source_message_id": str(row["source_message_id"])
                    if row["source_message_id"]
                    else None,
                    "hit_count": int(row["hit_count"]),
                    "last_hit_at": _iso(row["last_hit_at"]),
                    "created_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"]),
                }
                for row in rows
            ]

    async def delete_annotation(self, annotation_id: UUID) -> bool:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                sa.text("DELETE FROM annotation WHERE id = :id"), {"id": annotation_id}
            )
            return bool(getattr(result, "rowcount", 0))

    async def annotation_candidates(
        self,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        conversation_id: UUID,
        allow_conversation: bool,
        limit: int,
    ) -> tuple[tuple[UUID, str, float, float], ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, answer, threshold,
                               1 - (embedding <=> CAST(:embedding AS vector)) AS score
                        FROM annotation
                        WHERE enabled AND embedding IS NOT NULL
                          AND embedding_model = :embedding_model
                          AND (
                            scope = 'GLOBAL' OR
                            (:allow_conversation AND scope = 'CONVERSATION'
                             AND conversation_id = :conversation_id)
                          )
                        ORDER BY embedding <=> CAST(:embedding AS vector), id
                        LIMIT :limit
                        """
                    ),
                    {
                        "embedding": _vector_literal(query_embedding),
                        "embedding_model": embedding_model,
                        "allow_conversation": allow_conversation,
                        "conversation_id": conversation_id,
                        "limit": limit,
                    },
                )
            ).mappings().all()
            return tuple(
                (
                    row["id"],
                    row["answer"],
                    max(0.0, min(1.0, float(row["score"]))),
                    float(row["threshold"]),
                )
                for row in rows
            )

    async def mark_annotation_hit(self, annotation_id: UUID) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE annotation
                    SET hit_count = hit_count + 1, last_hit_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                    """
                ),
                {"id": annotation_id},
            )

    async def record_annotation_match(
        self,
        *,
        conversation_id: UUID,
        query_hash: str,
        annotation_id: UUID | None,
        score: float | None,
        threshold: float | None,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO annotation_match_audit (
                        id, conversation_id, annotation_id, query_hash,
                        score, threshold, outcome, error_code
                    ) VALUES (
                        :id, :conversation_id, :annotation_id, :query_hash,
                        :score, :threshold, :outcome, :error_code
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "conversation_id": conversation_id,
                    "annotation_id": annotation_id,
                    "query_hash": query_hash,
                    "score": score,
                    "threshold": threshold,
                    "outcome": outcome,
                    "error_code": error_code,
                },
            )

    async def annotation_metrics(self, *, hours: int = 24) -> dict[str, JsonValue]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT outcome, count(*) AS n
                        FROM annotation_match_audit
                        WHERE created_at >= CURRENT_TIMESTAMP - (:hours * INTERVAL '1 hour')
                        GROUP BY outcome
                        """
                    ),
                    {"hours": max(1, min(hours, 24 * 90))},
                )
            ).mappings().all()
        counts = {str(row["outcome"]): int(row["n"]) for row in rows}
        total = sum(counts.values())
        hits = counts.get("HIT", 0)
        return {
            "hit": hits,
            "miss": counts.get("MISS", 0),
            "error": counts.get("ERROR", 0),
            "total": total,
            "hit_rate": hits / total if total else 0.0,
        }

    @staticmethod
    def _document(row: RowMapping) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row["id"],
            title=row["title"],
            source_type=KnowledgeSourceType(row["source_type"]),
            scope=KnowledgeScope(row["scope"]),
            conversation_id=row["conversation_id"],
            status=KnowledgeDocumentStatus(row["status"]),
            content_hash=row["content_hash"],
            original_filename=row["original_filename"],
            generation=int(row["generation"]),
        )


__all__ = ["KnowledgeRepository"]
