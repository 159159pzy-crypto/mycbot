from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from mybot.contracts import AnnotationMatch, KnowledgeSourceType
from mybot.engine.knowledge import (
    AnnotationMatcher,
    IngestClaim,
    IngestClaimStatus,
    IngestSource,
    KnowledgeIngestor,
    PreparedParent,
    extract_text,
    hierarchical_chunks,
)
from mybot.infrastructure.model_routing import EmbeddingBatch


def _minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(payload)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(document)


def test_pdf_text_is_extracted_for_knowledge_ingestion() -> None:
    text = extract_text(
        KnowledgeSourceType.PDF,
        _minimal_pdf("MyBot handbook acceptance source"),
    )

    assert "MyBot handbook acceptance source" in text


def test_hierarchical_chunks_match_children_and_return_parent_context() -> None:
    text = "\n\n".join(f"第 {index} 段。" + "规则内容" * 50 for index in range(8))
    parents = hierarchical_chunks(text, parent_chars=320, child_chars=120, child_overlap=20)

    assert len(parents) > 1
    assert all(parent.children for parent in parents)
    assert all(child.content in parent.content for parent in parents for child in parent.children)
    assert all(child.token_count > 0 for parent in parents for child in parent.children)


@dataclass
class FakeEmbeddings:
    calls: list[list[str]] = field(default_factory=list)

    async def embed_with_model(self, texts):  # type: ignore[no-untyped-def]
        values = list(texts)
        self.calls.append(values)
        return EmbeddingBatch(
            vectors=[[float(index + 1), 0.5] for index, _ in enumerate(values)],
            model="actual-embed-v2",
        )


@dataclass
class FakeIngestStore:
    source: IngestSource | None
    completed: list[tuple[str, tuple[PreparedParent, ...], int, str]] = field(
        default_factory=list
    )
    failed: list[str] = field(default_factory=list)

    async def claim_ingestion(self, document_id: UUID, generation: int):  # type: ignore[no-untyped-def]
        source, self.source = self.source, None
        return IngestClaim(
            IngestClaimStatus.ACQUIRED if source is not None else IngestClaimStatus.TERMINAL,
            source,
        )

    async def complete_ingestion(self, source, **kwargs):  # type: ignore[no-untyped-def]
        self.completed.append(
            (
                kwargs["raw_text"],
                tuple(kwargs["parents"]),
                len(kwargs["child_embeddings"]),
                kwargs["embedding_model"],
            )
        )
        return True

    async def fail_ingestion(self, document_id, generation, *, error_code):  # type: ignore[no-untyped-def]
        self.failed.append(error_code)


@pytest.mark.asyncio
async def test_ingestor_is_idempotent_when_task_cannot_claim() -> None:
    store = FakeIngestStore(
        IngestSource(
            document_id=uuid4(), generation=1, source_type="txt", content="你好知识库".encode()
        )
    )
    embeddings = FakeEmbeddings()
    ingestor = KnowledgeIngestor(store=store, embeddings=embeddings)

    assert (
        await ingestor.ingest(store.source.document_id, 1)  # type: ignore[union-attr]
        is IngestClaimStatus.ACQUIRED
    )
    assert len(store.completed) == 1
    assert store.completed[0][2] == len(embeddings.calls[0])
    assert store.completed[0][3] == "actual-embed-v2"
    assert await ingestor.ingest(uuid4(), 1) is IngestClaimStatus.TERMINAL
    assert len(store.completed) == 1


@dataclass
class FakeAnnotationStore:
    candidates: tuple[tuple[UUID, str, float, float], ...]
    audits: list[dict[str, object]] = field(default_factory=list)
    hits: list[UUID] = field(default_factory=list)

    async def annotation_candidates(self, **kwargs):  # type: ignore[no-untyped-def]
        return self.candidates

    async def record_annotation_match(self, **kwargs):  # type: ignore[no-untyped-def]
        self.audits.append(kwargs)

    async def mark_annotation_hit(self, annotation_id: UUID) -> None:
        self.hits.append(annotation_id)


@pytest.mark.asyncio
async def test_annotation_requires_threshold_and_margin() -> None:
    first, second = uuid4(), uuid4()
    store = FakeAnnotationStore(
        ((first, "审核答案", 0.95, 0.92), (second, "近似答案", 0.94, 0.92))
    )
    matcher = AnnotationMatcher(
        store=store,
        embeddings=FakeEmbeddings(),
        minimum_margin=0.03,
    )

    assert await matcher.match("问题", conversation_id=uuid4(), allow_conversation=True) is None
    assert store.audits[-1]["outcome"] == "MISS"
    assert store.hits == []

    store.candidates = ((first, "审核答案", 0.97, 0.92), (second, "近似答案", 0.90, 0.92))
    match = await matcher.match("问题", conversation_id=uuid4(), allow_conversation=True)
    assert match == AnnotationMatch(
        annotation_id=first, answer="审核答案", score=0.97, threshold=0.92
    )
    assert store.hits == [first]
    assert store.audits[-1]["outcome"] == "HIT"
