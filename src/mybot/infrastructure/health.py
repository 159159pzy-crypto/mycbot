"""Dependency probes and aggregate readiness behavior."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from math import ceil
from typing import Literal, Protocol, cast, runtime_checkable

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mybot.contracts.common import FrozenModel
from mybot.settings import Settings


class DependencyStatus(StrEnum):
    UP = "up"
    DOWN = "down"


class DependencyHealth(FrozenModel):
    status: DependencyStatus
    detail: str | None = None


class ReadinessDependencies(FrozenModel):
    database: DependencyHealth
    redis: DependencyHealth


class ReadinessResponse(FrozenModel):
    status: Literal["ready", "not_ready"]
    dependencies: ReadinessDependencies


class LivenessResponse(FrozenModel):
    status: Literal["alive"] = "alive"


class DependencyProbe(Protocol):
    async def check(self) -> None: ...


@runtime_checkable
class AsyncClosable(Protocol):
    async def aclose(self) -> None: ...


class AsyncRedisClient(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class DatabaseProbe:
    engine: AsyncEngine

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def aclose(self) -> None:
        await self.engine.dispose()


@dataclass(slots=True)
class RedisProbe:
    client: AsyncRedisClient

    async def check(self) -> None:
        await self.client.ping()

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass(slots=True)
class ReadinessService:
    database: DependencyProbe
    redis: DependencyProbe
    probe_timeout_seconds: float = 2.0

    async def _check_probe(self, probe: DependencyProbe) -> DependencyHealth:
        try:
            async with asyncio.timeout(self.probe_timeout_seconds):
                await probe.check()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return DependencyHealth(status=DependencyStatus.DOWN, detail=type(error).__name__)
        return DependencyHealth(status=DependencyStatus.UP)

    async def check(self) -> ReadinessResponse:
        database, redis = await asyncio.gather(
            self._check_probe(self.database),
            self._check_probe(self.redis),
        )
        ready = database.status is DependencyStatus.UP and redis.status is DependencyStatus.UP
        return ReadinessResponse(
            status="ready" if ready else "not_ready",
            dependencies=ReadinessDependencies(database=database, redis=redis),
        )

    async def aclose(self) -> None:
        for probe in (self.database, self.redis):
            if isinstance(probe, AsyncClosable):
                await probe.aclose()


def create_readiness_service(settings: Settings) -> ReadinessService:
    database_url = settings.database_url.get_secret_value()
    redis_url = settings.redis_url.get_secret_value()
    redis_client = cast(
        AsyncRedisClient,
        Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_read_timeout_seconds,
        ),
    )
    return ReadinessService(
        database=DatabaseProbe(
            create_async_engine(
                database_url,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={
                    "connect_timeout": ceil(settings.database_connect_timeout_seconds),
                    "options": (
                        "-c statement_timeout="
                        f"{ceil(settings.database_read_timeout_seconds * 1000)}"
                    ),
                },
            )
        ),
        redis=RedisProbe(redis_client),
        probe_timeout_seconds=settings.health_probe_timeout_seconds,
    )
