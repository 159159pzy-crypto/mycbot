from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from mybot.contracts import KnowledgeIngestTask
from mybot.engine.knowledge import IngestClaimStatus
from mybot.infrastructure.streams import MemoryStreamBackend, StreamConsumer, StreamPublisher
from mybot.services.knowledge import KnowledgeOutboxDispatcher, KnowledgeWorkerService


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class ScriptedIngestor:
    outcomes: list[IngestClaimStatus]
    calls: int = 0

    async def ingest(self, document_id: UUID, generation: int) -> IngestClaimStatus:
        self.calls += 1
        return self.outcomes.pop(0)


@pytest.mark.asyncio
async def test_busy_knowledge_lease_stays_pending_until_reclaim() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    consumer = StreamConsumer(
        backend,
        stream="mybot:knowledge",
        group="knowledge-workers",
        consumer="worker-1",
        dead_letter_stream="mybot:knowledge:dead",
        max_attempts=3,
        dedupe_ttl_seconds=60,
        dedupe_prefix="mybot:seen:knowledge",
        block_ms=10,
        claim_min_idle_ms=5_000,
    )
    ingestor = ScriptedIngestor(
        [IngestClaimStatus.BUSY, IngestClaimStatus.ACQUIRED]
    )
    service = KnowledgeWorkerService(consumer=consumer, ingestor=ingestor)  # type: ignore[arg-type]
    task = KnowledgeIngestTask(document_id=uuid4(), generation=1)
    await StreamPublisher(backend, "mybot:knowledge", 100).publish(task.model_dump_json())

    await consumer.process_available(service.handle_payload)
    assert await backend.pending_count("mybot:knowledge", "knowledge-workers") == 1

    clock.now += 10.0
    await consumer.process_available(service.handle_payload)

    assert ingestor.calls == 2
    assert await backend.pending_count("mybot:knowledge", "knowledge-workers") == 0


@dataclass
class FakeOutboxRepository:
    task: tuple[UUID, int]
    failed: list[tuple[UUID, int, str]] = field(default_factory=list)
    published: list[tuple[UUID, int]] = field(default_factory=list)

    async def pending_ingest_tasks(self, *, limit=50, document_id=None):  # type: ignore[no-untyped-def]
        return () if self.published else (self.task,)

    async def mark_ingest_task_failed(
        self, document_id: UUID, generation: int, *, error_code: str
    ) -> None:
        self.failed.append((document_id, generation, error_code))

    async def mark_ingest_task_published(self, document_id: UUID, generation: int) -> None:
        self.published.append((document_id, generation))


@dataclass
class FailOncePublisher:
    calls: int = 0

    async def publish(self, payload: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("redis unavailable")
        return "1-0"


@pytest.mark.asyncio
async def test_outbox_retains_failed_publish_and_retries_it() -> None:
    task = (uuid4(), 1)
    repository = FakeOutboxRepository(task)
    publisher = FailOncePublisher()
    dispatcher = KnowledgeOutboxDispatcher(
        repository=repository,  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
    )

    assert await dispatcher.flush_once() == 0
    assert repository.failed == [(task[0], 1, "RuntimeError")]
    assert repository.published == []

    assert await dispatcher.flush_once() == 1
    assert repository.published == [task]
