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
        if self.proactive is not None:
            try:
                await self.proactive.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("proactive_pass_failed")


def create_maintenance_service(settings: Settings) -> MaintenanceWorkerService:
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.streams import StreamPublisher, create_redis_backend
    from mybot.repositories.conversations import ConversationRepository
    from mybot.repositories.memory import MemoryRepository
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.system_kv import SystemKvRepository
    from mybot.repositories.turns import TurnRepository
    from mybot.services.proactive import ProactivePass

    sessions = create_session_factory(create_database_engine(settings))
    backend = create_redis_backend(settings.redis_url.get_secret_value())
    proactive = ProactivePass(
        config=SystemKvRepository(sessions),
        conversations=ConversationRepository(sessions),
        messages=MessageRepository(sessions),
        turns=TurnRepository(sessions),
        outbound=StreamPublisher(
            backend=backend,
            stream=settings.outbound_stream,
            maxlen=settings.stream_maxlen,
        ),
        once=backend,
        enabled=settings.proactive_enabled,
        message=settings.proactive_message,
        min_interval_hours=settings.proactive_min_interval_hours,
        quiet_start_hour=settings.proactive_quiet_start_hour,
        quiet_end_hour=settings.proactive_quiet_end_hour,
    )
    return MaintenanceWorkerService(
        memory=MemoryRepository(sessions),
        interval_seconds=settings.memory_maintenance_interval_seconds,
        decay_days=settings.memory_decay_days,
        decay_factor=settings.memory_decay_factor,
        confidence_floor=settings.memory_confidence_floor,
        revoked_retention_days=settings.memory_revoked_retention_days,
        proactive=proactive,
    )
