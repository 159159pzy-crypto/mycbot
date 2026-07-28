"""Renewable Redis leases used to serialize one conversation across workers."""

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol, TypeVar, cast

import structlog
from redis.asyncio import Redis

T = TypeVar("T")
logger = structlog.get_logger("mybot.leases")

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the lease protecting its work."""


class LeaseBackend(Protocol):
    async def acquire(self, key: str, token: str, *, ttl_ms: int) -> bool: ...

    async def renew(self, key: str, token: str, *, ttl_ms: int) -> bool: ...

    async def release(self, key: str, token: str) -> bool: ...


class ConversationLease(Protocol):
    async def run(self, stable_key: str, work: Callable[[], Awaitable[T]]) -> T: ...


@dataclass(slots=True)
class LeaseManager:
    backend: LeaseBackend
    ttl_ms: int = 30_000
    wait_timeout_seconds: float = 180.0
    retry_interval_seconds: float = 0.05
    prefix: str = "mybot:conversation-lease"

    async def run(self, stable_key: str, work: Callable[[], Awaitable[T]]) -> T:
        key = self._key(stable_key)
        token = secrets.token_urlsafe(24)
        deadline = monotonic() + self.wait_timeout_seconds
        while not await self.backend.acquire(key, token, ttl_ms=self.ttl_ms):
            if monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the conversation lease")
            await asyncio.sleep(self.retry_interval_seconds)

        lost = asyncio.Event()
        owner = asyncio.current_task()
        assert owner is not None
        renewer = asyncio.create_task(
            self._renew(key, token, lost, owner), name="lease-renewal"
        )
        try:
            result = await work()
            if lost.is_set():
                raise LeaseLostError("conversation lease renewal failed")
            return result
        except asyncio.CancelledError as error:
            if lost.is_set():
                raise LeaseLostError("conversation lease renewal failed") from error
            raise
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            try:
                await self.backend.release(key, token)
            except Exception:
                logger.exception("conversation_lease_release_failed", lease_key=key)

    async def _renew(
        self,
        key: str,
        token: str,
        lost: asyncio.Event,
        owner: asyncio.Task[object],
    ) -> None:
        interval = max(self.ttl_ms / 3 / 1_000, 0.01)
        while True:
            await asyncio.sleep(interval)
            try:
                owned = await self.backend.renew(key, token, ttl_ms=self.ttl_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                owned = False
            if not owned:
                lost.set()
                owner.cancel()
                return

    def _key(self, stable_key: str) -> str:
        digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
        return f"{self.prefix}:{digest}"


@dataclass(slots=True)
class MemoryLeaseBackend:
    """Process-local lease backend for unit tests and explicitly local services."""

    clock: Callable[[], float] = monotonic
    _leases: dict[str, tuple[str, float]] = field(default_factory=dict[str, tuple[str, float]])
    _guard: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, key: str, token: str, *, ttl_ms: int) -> bool:
        async with self._guard:
            now = self.clock()
            current = self._leases.get(key)
            if current is not None and current[1] > now:
                return False
            self._leases[key] = (token, now + ttl_ms / 1_000)
            return True

    async def renew(self, key: str, token: str, *, ttl_ms: int) -> bool:
        async with self._guard:
            current = self._leases.get(key)
            if current is None or current[0] != token or current[1] <= self.clock():
                return False
            self._leases[key] = (token, self.clock() + ttl_ms / 1_000)
            return True

    async def release(self, key: str, token: str) -> bool:
        async with self._guard:
            current = self._leases.get(key)
            if current is None or current[0] != token:
                return False
            self._leases.pop(key, None)
            return True


class RedisLeaseBackend:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def acquire(self, key: str, token: str, *, ttl_ms: int) -> bool:
        return bool(await self._client.set(key, token, nx=True, px=ttl_ms))

    async def renew(self, key: str, token: str, *, ttl_ms: int) -> bool:
        result = await cast(
            Awaitable[object],
            self._client.eval(_RENEW_SCRIPT, 1, key, token, str(ttl_ms)),
        )
        return bool(result)

    async def release(self, key: str, token: str) -> bool:
        result = await cast(
            Awaitable[object], self._client.eval(_RELEASE_SCRIPT, 1, key, token)
        )
        return bool(result)


def create_redis_lease_manager(
    redis_url: str,
    *,
    ttl_ms: int,
    wait_timeout_seconds: float,
    retry_interval_seconds: float,
) -> LeaseManager:
    client = Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    return LeaseManager(
        RedisLeaseBackend(client),
        ttl_ms=ttl_ms,
        wait_timeout_seconds=wait_timeout_seconds,
        retry_interval_seconds=retry_interval_seconds,
    )


__all__ = [
    "ConversationLease",
    "LeaseLostError",
    "LeaseManager",
    "MemoryLeaseBackend",
    "RedisLeaseBackend",
    "create_redis_lease_manager",
]
