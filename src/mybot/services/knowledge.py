"""Independent Redis Streams worker for knowledge document ingestion."""

import asyncio
import os
from dataclasses import dataclass

import structlog

from mybot.contracts import KnowledgeIngestTask
from mybot.engine.knowledge import IngestClaimStatus, KnowledgeIngestor
from mybot.infrastructure.streams import StreamConsumer, StreamPublisher, create_redis_backend
from mybot.repositories.knowledge import KnowledgeRepository
from mybot.runtime import ProcessMode
from mybot.settings import Settings

logger = structlog.get_logger("mybot.knowledge_worker")


class KnowledgeLeaseBusy(RuntimeError):
    """The task is valid but another worker still owns its database lease."""


@dataclass(slots=True)
class KnowledgeOutboxDispatcher:
    repository: KnowledgeRepository
    publisher: StreamPublisher
    poll_seconds: float = 5.0

    async def flush_once(self) -> int:
        published = 0
        for document_id, generation in await self.repository.pending_ingest_tasks():
            try:
                await self.publisher.publish(
                    KnowledgeIngestTask(
                        document_id=document_id, generation=generation
                    ).model_dump_json()
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.repository.mark_ingest_task_failed(
                    document_id, generation, error_code=type(error).__name__
                )
                logger.warning(
                    "knowledge_outbox_publish_failed",
                    document_id=str(document_id),
                    generation=generation,
                    error_code=type(error).__name__,
                )
                continue
            await self.repository.mark_ingest_task_published(document_id, generation)
            published += 1
        return published

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("knowledge_outbox_iteration_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


@dataclass(slots=True)
class KnowledgeWorkerService:
    consumer: StreamConsumer
    ingestor: KnowledgeIngestor
    outbox: KnowledgeOutboxDispatcher | None = None

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger.info("knowledge_worker_started", mode=mode.value)
        tasks = [
            asyncio.create_task(
                self.consumer.run(self.handle_payload, stop_event=stop_event),
                name="knowledge-consumer",
            )
        ]
        if self.outbox is not None:
            tasks.append(
                asyncio.create_task(self.outbox.run(stop_event), name="knowledge-outbox")
            )
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("knowledge_worker_stopped", mode=mode.value)

    async def handle_payload(self, payload: str) -> None:
        task = KnowledgeIngestTask.model_validate_json(payload)
        outcome = await self.ingestor.ingest(task.document_id, task.generation)
        if outcome is IngestClaimStatus.BUSY:
            raise KnowledgeLeaseBusy("knowledge ingestion lease is still active")


def create_knowledge_worker_service(settings: Settings) -> KnowledgeWorkerService:
    import httpx
    from redis.asyncio import Redis

    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.model_routing import (
        ModelRouter,
        RedisModelCooldowns,
        legacy_model_channels,
        openai_client_factory,
    )
    from mybot.repositories.llm_calls import LlmCallLogRepository
    from mybot.repositories.system_kv import SystemKvRepository

    backend = create_redis_backend(settings.redis_url.get_secret_value())
    sessions = create_session_factory(create_database_engine(settings))
    router = ModelRouter(
        config=SystemKvRepository(sessions),
        fallback_channels=legacy_model_channels(settings),
        client_factory=openai_client_factory(
            httpx.AsyncClient(),
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        attempts=LlmCallLogRepository(sessions),
        cooldowns=RedisModelCooldowns(
            Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(), decode_responses=True
            )
        ),
        secret_lookup=settings.model_secret,
        cache_ttl_seconds=settings.model_channels_cache_ttl_seconds,
        cooldown_seconds=settings.model_channel_cooldown_seconds,
    )
    repository = KnowledgeRepository(
        sessions, ingestion_lease_seconds=settings.knowledge_ingestion_lease_seconds
    )
    publisher = StreamPublisher(
        backend=backend, stream=settings.knowledge_stream, maxlen=settings.stream_maxlen
    )
    return KnowledgeWorkerService(
        consumer=StreamConsumer(
            backend,
            stream=settings.knowledge_stream,
            group=settings.knowledge_group,
            consumer=f"knowledge-worker-{os.getpid()}",
            dead_letter_stream=f"{settings.knowledge_stream}:dead",
            max_attempts=settings.stream_delivery_max_attempts,
            dedupe_ttl_seconds=settings.stream_dedupe_ttl_seconds,
            dedupe_prefix="mybot:seen:knowledge",
            block_ms=settings.stream_block_ms,
            claim_min_idle_ms=max(
                settings.stream_claim_min_idle_ms,
                settings.knowledge_ingestion_lease_seconds * 1_000 + 1_000,
            ),
        ),
        ingestor=KnowledgeIngestor(
            store=repository,
            embeddings=router.embeddings(),
            parent_chars=settings.knowledge_parent_chunk_chars,
            child_chars=settings.knowledge_child_chunk_chars,
            child_overlap=settings.knowledge_child_chunk_overlap,
        ),
        outbox=KnowledgeOutboxDispatcher(
            repository=repository,
            publisher=publisher,
            poll_seconds=settings.knowledge_outbox_poll_seconds,
        ),
    )


__all__ = [
    "KnowledgeLeaseBusy",
    "KnowledgeOutboxDispatcher",
    "KnowledgeWorkerService",
    "create_knowledge_worker_service",
]
