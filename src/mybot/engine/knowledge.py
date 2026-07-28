"""Hierarchical document ingestion, retrieval, and annotation matching."""

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import ceil
from typing import Protocol
from uuid import UUID

from mybot.contracts import (
    AnnotationMatch,
    KnowledgeSearchHit,
    KnowledgeSourceType,
)
from mybot.infrastructure.model_routing import EmbeddingBatch


class Embeddings(Protocol):
    async def embed_with_model(self, texts: Sequence[str]) -> EmbeddingBatch: ...


@dataclass(slots=True, frozen=True)
class PreparedChild:
    index: int
    content: str
    token_count: int


@dataclass(slots=True, frozen=True)
class PreparedParent:
    index: int
    content: str
    token_count: int
    children: tuple[PreparedChild, ...]


@dataclass(slots=True, frozen=True)
class IngestSource:
    document_id: UUID
    generation: int
    source_type: KnowledgeSourceType
    content: bytes


class IngestClaimStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    BUSY = "BUSY"
    TERMINAL = "TERMINAL"


@dataclass(slots=True, frozen=True)
class IngestClaim:
    status: IngestClaimStatus
    source: IngestSource | None = None


class KnowledgeStore(Protocol):
    async def claim_ingestion(self, document_id: UUID, generation: int) -> IngestClaim: ...

    async def complete_ingestion(
        self,
        source: IngestSource,
        *,
        raw_text: str,
        parents: Sequence[PreparedParent],
        child_embeddings: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> bool: ...

    async def fail_ingestion(
        self, document_id: UUID, generation: int, *, error_code: str
    ) -> None: ...

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
    ) -> tuple[KnowledgeSearchHit, ...]: ...


class AnnotationStore(Protocol):
    async def annotation_candidates(
        self,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        conversation_id: UUID,
        allow_conversation: bool,
        limit: int,
    ) -> tuple[tuple[UUID, str, float, float], ...]: ...

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
    ) -> None: ...

    async def mark_annotation_hit(self, annotation_id: UUID) -> None: ...


def extract_text(source_type: KnowledgeSourceType, content: bytes) -> str:
    if source_type in {KnowledgeSourceType.MARKDOWN, KnowledgeSourceType.TEXT}:
        return content.decode("utf-8-sig", errors="replace").strip()
    if source_type is KnowledgeSourceType.PDF:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    raise ValueError(f"unsupported source type: {source_type}")


def hierarchical_chunks(
    text: str,
    *,
    parent_chars: int = 2_400,
    child_chars: int = 700,
    child_overlap: int = 100,
) -> tuple[PreparedParent, ...]:
    cleaned = re.sub(r"\r\n?", "\n", text).strip()
    if not cleaned:
        return ()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    parent_texts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if current and len(candidate) > parent_chars:
            parent_texts.extend(_hard_split(current, parent_chars, overlap=0))
            current = paragraph
        else:
            current = candidate
    if current:
        parent_texts.extend(_hard_split(current, parent_chars, overlap=0))

    parents: list[PreparedParent] = []
    for parent_index, parent in enumerate(parent_texts):
        child_texts = _hard_split(parent, child_chars, overlap=child_overlap)
        children = tuple(
            PreparedChild(index=index, content=value, token_count=_tokens(value))
            for index, value in enumerate(child_texts)
            if value.strip()
        )
        if children:
            parents.append(
                PreparedParent(
                    index=parent_index,
                    content=parent,
                    token_count=_tokens(parent),
                    children=children,
                )
            )
    return tuple(parents)


def _hard_split(text: str, size: int, *, overlap: int) -> list[str]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("invalid chunk size or overlap")
    if len(text) <= size:
        return [text.strip()]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        value = text[start:end].strip()
        if value:
            chunks.append(value)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _tokens(text: str) -> int:
    return max(1, ceil(len(text) / 4))


@dataclass(slots=True)
class KnowledgeIngestor:
    store: KnowledgeStore
    embeddings: Embeddings
    parent_chars: int = 2_400
    child_chars: int = 700
    child_overlap: int = 100

    async def ingest(self, document_id: UUID, generation: int) -> IngestClaimStatus:
        claim = await self.store.claim_ingestion(document_id, generation)
        if claim.status is not IngestClaimStatus.ACQUIRED:
            return claim.status
        assert claim.source is not None
        source = claim.source
        try:
            text = extract_text(source.source_type, source.content)
            parents = hierarchical_chunks(
                text,
                parent_chars=self.parent_chars,
                child_chars=self.child_chars,
                child_overlap=self.child_overlap,
            )
            if not parents:
                raise ValueError("document contains no extractable text")
            child_texts = [child.content for parent in parents for child in parent.children]
            batch = await self.embeddings.embed_with_model(child_texts)
            if len(batch.vectors) != len(child_texts):
                raise ValueError("embedding count does not match child count")
            completed = await self.store.complete_ingestion(
                source,
                raw_text=text,
                parents=parents,
                child_embeddings=batch.vectors,
                embedding_model=batch.model,
            )
            return (
                IngestClaimStatus.ACQUIRED
                if completed
                else IngestClaimStatus.TERMINAL
            )
        except Exception as error:
            await self.store.fail_ingestion(
                document_id, generation, error_code=type(error).__name__
            )
            raise


@dataclass(slots=True)
class KnowledgeSearchService:
    store: KnowledgeStore
    embeddings: Embeddings

    async def search(
        self,
        query: str,
        *,
        conversation_stable_key: str | None,
        allow_conversation: bool,
        include_global: bool = True,
        top_k: int = 5,
        threshold: float = 0.35,
    ) -> tuple[KnowledgeSearchHit, ...]:
        batch = await self.embeddings.embed_with_model([query])
        if not batch.vectors:
            return ()
        return await self.store.search(
            query_embedding=batch.vectors[0],
            embedding_model=batch.model,
            conversation_stable_key=conversation_stable_key,
            include_global=include_global,
            allow_conversation=allow_conversation,
            top_k=max(1, min(top_k, 20)),
            threshold=max(0.0, min(threshold, 1.0)),
        )


@dataclass(slots=True)
class AnnotationMatcher:
    store: AnnotationStore
    embeddings: Embeddings
    minimum_margin: float = 0.03

    async def match(
        self,
        query: str,
        *,
        conversation_id: UUID,
        allow_conversation: bool,
    ) -> AnnotationMatch | None:
        query_hash = sha256(query.strip().encode()).hexdigest()
        try:
            batch = await self.embeddings.embed_with_model([query])
            candidates = await self.store.annotation_candidates(
                query_embedding=batch.vectors[0],
                embedding_model=batch.model,
                conversation_id=conversation_id,
                allow_conversation=allow_conversation,
                limit=2,
            )
            best = candidates[0] if candidates else None
            second_score = candidates[1][2] if len(candidates) > 1 else 0.0
            if best is None or best[2] < best[3] or best[2] - second_score < self.minimum_margin:
                await self.store.record_annotation_match(
                    conversation_id=conversation_id,
                    query_hash=query_hash,
                    annotation_id=best[0] if best else None,
                    score=best[2] if best else None,
                    threshold=best[3] if best else None,
                    outcome="MISS",
                )
                return None
            await self.store.mark_annotation_hit(best[0])
            await self.store.record_annotation_match(
                conversation_id=conversation_id,
                query_hash=query_hash,
                annotation_id=best[0],
                score=best[2],
                threshold=best[3],
                outcome="HIT",
            )
            return AnnotationMatch(
                annotation_id=best[0], answer=best[1], score=best[2], threshold=best[3]
            )
        except Exception as error:
            await self.store.record_annotation_match(
                conversation_id=conversation_id,
                query_hash=query_hash,
                annotation_id=None,
                score=None,
                threshold=None,
                outcome="ERROR",
                error_code=type(error).__name__,
            )
            return None


__all__ = [
    "AnnotationMatcher",
    "IngestClaim",
    "IngestClaimStatus",
    "IngestSource",
    "KnowledgeIngestor",
    "KnowledgeSearchService",
    "PreparedChild",
    "PreparedParent",
    "extract_text",
    "hierarchical_chunks",
]
