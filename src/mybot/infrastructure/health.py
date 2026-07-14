"""Dependency probes and aggregate readiness behavior."""

import asyncio
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mybot.settings import Settings


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

    async def check(self) -> tuple[bool, dict[str, dict[str, str]]]:
        results = await asyncio.gather(
            self.database.check(),
            self.redis.check(),
            return_exceptions=True,
        )
        dependencies: dict[str, dict[str, str]] = {}
        for name, result in zip(("database", "redis"), results, strict=True):
            if isinstance(result, BaseException):
                dependencies[name] = {
                    "status": "down",
                    "detail": type(result).__name__,
                }
            else:
                dependencies[name] = {"status": "up"}
        return all(result is None for result in results), dependencies

    async def aclose(self) -> None:
        for probe in (self.database, self.redis):
            if isinstance(probe, AsyncClosable):
                await probe.aclose()


def create_readiness_service(settings: Settings) -> ReadinessService:
    database_url = settings.database_url.get_secret_value()
    redis_url = settings.redis_url.get_secret_value()
    redis_client = cast(
        AsyncRedisClient,
        Redis.from_url(redis_url, decode_responses=True),  # pyright: ignore[reportUnknownMemberType]
    )
    return ReadinessService(
        database=DatabaseProbe(
            create_async_engine(database_url, pool_pre_ping=True, pool_recycle=300)
        ),
        redis=RedisProbe(redis_client),
    )
