"""The maintenance-worker lifecycle: periodic memory expiry, decay, cleanup."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import structlog

from mybot.repositories.memory import LifecycleReport
from mybot.runtime import ProcessMode
from mybot.settings import Settings

logger = structlog.get_logger("mybot.maintenance")


class MemoryMaintenance(Protocol):
    async def run_lifecycle(
        self,
        *,
        now: datetime,
        decay_days: int,
        decay_factor: float,
        confidence_floor: float,
        revoked_retention_days: int,
    ) -> LifecycleReport: ...


class ProactiveJob(Protocol):
    async def run_pass(self) -> int: ...


class ConsolidationJob(Protocol):
    async def run_pass(self) -> int: ...


class PersonalityJob(Protocol):
    async def run_pass(self) -> int: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class MaintenanceWorkerService:
    """LifecycleService running the memory lifecycle pass on an interval."""

    memory: MemoryMaintenance
    interval_seconds: float
    decay_days: int
    decay_factor: float
    confidence_floor: float
    revoked_retention_days: int
    consolidation: ConsolidationJob | None = None
    personality: PersonalityJob | None = None
    proactive: ProactiveJob | None = None
    now: Callable[[], datetime] = field(default=_utc_now)

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger.info("maintenance_worker_started", mode=mode.value)
        try:
            while not stop_event.is_set():
                await self._run_pass()
                try:
                    async with asyncio.timeout(self.interval_seconds):
                        await stop_event.wait()
                except TimeoutError:
                    continue
        finally:
            logger.info("maintenance_worker_stopped", mode=mode.value)

    async def _run_pass(self) -> None:
        try:
            report = await self.memory.run_lifecycle(
                now=self.now(),
                decay_days=self.decay_days,
                decay_factor=self.decay_factor,
                confidence_floor=self.confidence_floor,
                revoked_retention_days=self.revoked_retention_days,
            )
            logger.info(
                "memory_lifecycle_pass_completed",
                expired=report.expired,
                decayed=report.decayed,
                deleted=report.deleted,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory_lifecycle_pass_failed")
        if self.consolidation is not None:
            try:
                await self.consolidation.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("memory_consolidation_pass_failed")
        if self.personality is not None:
            try:
                await self.personality.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("personality_learning_pass_failed")
        if self.proactive is not None:
            try:
                await self.proactive.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("proactive_pass_failed")


def create_maintenance_service(settings: Settings) -> MaintenanceWorkerService:
    import httpx

    from mybot.engine.memory_consolidation import MemoryConsolidationJob
    from mybot.engine.memory_service import MemoryService
    from mybot.engine.personality_learning import PersonalityLearningJob
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.model_routing import (
        ModelPurpose,
        ModelRouter,
        RedisModelCooldowns,
        legacy_model_channels,
        openai_client_factory,
    )
    from mybot.infrastructure.streams import StreamPublisher, create_redis_backend
    from mybot.repositories.conversations import ConversationRepository
    from mybot.repositories.llm_calls import LlmCallLogRepository
    from mybot.repositories.memory import MemoryRepository
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.participation import ProactiveGenerationAuditRepository
    from mybot.repositories.profiles import ProfileRepository
    from mybot.repositories.system_kv import SystemKvRepository
    from mybot.repositories.turns import TurnRepository
    from mybot.services.proactive import ProactivePass

    sessions = create_session_factory(create_database_engine(settings))
    backend = create_redis_backend(settings.redis_url.get_secret_value())
    memory_repository = MemoryRepository(sessions)
    messages = MessageRepository(sessions)
    config = SystemKvRepository(sessions)
    model_router = None
    memory_llm = None
    background_memory = None
    consolidation = None
    if (
        (settings.memory_enabled and settings.memory_consolidation_enabled)
        or (settings.memory_enabled and settings.personality_learning_enabled)
        or settings.proactive_enabled
    ):
        from redis.asyncio import Redis

        model_http = httpx.AsyncClient()
        model_router = ModelRouter(
            config=config,
            fallback_channels=legacy_model_channels(settings),
            cooldowns=RedisModelCooldowns(
                Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                    settings.redis_url.get_secret_value(), decode_responses=True
                )
            ),
            attempts=LlmCallLogRepository(sessions),
            client_factory=openai_client_factory(
                model_http,
                temperature=0.0,
                max_output_tokens=settings.llm_max_output_tokens,
                timeout_seconds=settings.llm_timeout_seconds,
            ),
            secret_lookup=settings.model_secret,
            cache_ttl_seconds=settings.model_channels_cache_ttl_seconds,
            cooldown_seconds=settings.model_channel_cooldown_seconds,
        )
        memory_llm = model_router.for_purpose(ModelPurpose.MEMORY)
        if settings.memory_enabled:
            background_memory = MemoryService(
                embeddings=model_router.embeddings(),
                store=memory_repository,
                llm=memory_llm,
                min_confidence=settings.memory_min_confidence,
                retrieval_limit=settings.memory_retrieval_limit,
                token_budget=settings.memory_token_budget,
            )
        if settings.memory_consolidation_enabled and background_memory is not None:
            consolidation = MemoryConsolidationJob(
                source=memory_repository,
                memory=background_memory,
                llm=memory_llm,
                once=backend,
                interval_seconds=settings.memory_consolidation_interval_seconds,
                lookback_hours=settings.memory_consolidation_lookback_hours,
                conversation_limit=settings.memory_consolidation_conversation_limit,
                message_limit=settings.memory_consolidation_message_limit,
                memory_limit=settings.memory_consolidation_memory_limit,
                prompt_token_budget=settings.memory_consolidation_token_budget,
                min_confidence=settings.memory_min_confidence,
                enabled=True,
            )
    personality = (
        PersonalityLearningJob(
            source=messages,
            memory=background_memory,
            relationships=memory_repository,
            llm=memory_llm,
            once=backend,
            enabled=True,
            lookback_hours=settings.personality_learning_lookback_hours,
            conversation_limit=settings.personality_learning_conversation_limit,
            message_limit=settings.personality_learning_message_limit,
            prompt_token_budget=settings.personality_learning_token_budget,
            min_confidence=settings.personality_learning_min_confidence,
            dedupe_ttl_seconds=settings.personality_learning_dedupe_ttl_seconds,
        )
        if (
            settings.personality_learning_enabled
            and background_memory is not None
            and memory_llm is not None
        )
        else None
    )
    proactive = ProactivePass(
        config=config,
        conversations=ConversationRepository(sessions),
        messages=messages,
        turns=TurnRepository(sessions),
        outbound=StreamPublisher(
            backend=backend,
            stream=settings.outbound_stream,
            maxlen=settings.stream_maxlen,
        ),
        once=backend,
        history=messages,
        llm=memory_llm,
        profiles=ProfileRepository(sessions, legacy_persona=config),
        memory=background_memory,
        audit=ProactiveGenerationAuditRepository(sessions),
        enabled=settings.proactive_enabled,
        message=settings.proactive_message,
        default_system_prompt=settings.agent_system_prompt,
        default_tool_capabilities=settings.granted_capabilities(),
        context_messages=settings.proactive_context_messages,
        min_interval_hours=settings.proactive_min_interval_hours,
        quiet_start_hour=settings.proactive_quiet_start_hour,
        quiet_end_hour=settings.proactive_quiet_end_hour,
    )
    return MaintenanceWorkerService(
        memory=memory_repository,
        interval_seconds=settings.memory_maintenance_interval_seconds,
        decay_days=settings.memory_decay_days,
        decay_factor=settings.memory_decay_factor,
        confidence_floor=settings.memory_confidence_floor,
        revoked_retention_days=settings.memory_revoked_retention_days,
        consolidation=consolidation,
        personality=personality,
        proactive=proactive,
    )
