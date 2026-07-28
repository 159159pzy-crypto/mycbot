"""Bearer authentication for operator APIs with failure rate limiting."""

import hashlib
import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

import structlog
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger("mybot.operator.auth")

# The operator API is the only authenticated surface. Health probes and the
# internal plugin-control plane stay open (documented since Milestone 1).
_GUARDED_PREFIX = "/operator"


@dataclass(slots=True)
class AuthRateLimiter:
    """Counts recent auth failures per client; injectable clock for tests."""

    max_failures: int = 10
    window_seconds: float = 60.0
    clock: Callable[[], float] = field(default=monotonic)
    _failures: dict[str, deque[float]] = field(
        default_factory=dict[str, "deque[float]"], init=False
    )

    def _bucket(self, client: str) -> deque[float]:
        bucket = self._failures.get(client)
        if bucket is None:
            bucket = deque[float]()
            self._failures[client] = bucket
        cutoff = self.clock() - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    async def blocked(self, client: str) -> bool:
        return len(self._bucket(client)) >= self.max_failures

    async def record_failure(self, client: str) -> None:
        self._bucket(client).append(self.clock())


class AuthFailureLimiter(Protocol):
    async def blocked(self, client: str) -> bool: ...

    async def record_failure(self, client: str) -> None: ...


class RedisAuthRateLimiter:
    """Shared fixed-window auth limiter so API replica changes cannot bypass it."""

    def __init__(
        self,
        client: Redis,
        *,
        max_failures: int,
        window_seconds: float,
        prefix: str = "mybot:operator-auth",
    ) -> None:
        self._client = client
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.prefix = prefix

    async def blocked(self, client: str) -> bool:
        value = await self._client.get(self._key(client))
        try:
            return int(value or 0) >= self.max_failures
        except (TypeError, ValueError):
            return False

    async def record_failure(self, client: str) -> None:
        key = self._key(client)
        value = await self._client.incr(key)
        if int(value) == 1:
            await self._client.pexpire(key, int(self.window_seconds * 1_000))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _key(self, client: str) -> str:
        digest = hashlib.sha256(client.encode("utf-8")).hexdigest()
        return f"{self.prefix}:{digest}"


def create_redis_auth_limiter(
    redis_url: str, *, max_failures: int, window_seconds: float
) -> RedisAuthRateLimiter:
    client = Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    return RedisAuthRateLimiter(
        client,
        max_failures=max_failures,
        window_seconds=window_seconds,
    )


@dataclass(slots=True)
class OperatorAuth:
    """Policy: fail closed with no token; health and broker surfaces stay open."""

    token: str | None
    limiter: AuthFailureLimiter

    async def guard(self, request: Request) -> Response | None:
        """Return a refusal response, or None to let the request through."""

        path = request.url.path
        if not (path == _GUARDED_PREFIX or path.startswith(f"{_GUARDED_PREFIX}/")):
            return None
        client = request.client.host if request.client else "unknown"
        if await self.limiter.blocked(client):
            return JSONResponse(
                {"detail": "too many failed authentication attempts"}, status_code=429
            )
        if self.token is None:
            return JSONResponse(
                {"detail": "the operator API is disabled: MYBOT_OPERATOR_TOKEN is not set"},
                status_code=403,
            )
        header = request.headers.get("authorization", "")
        provided = header.removeprefix("Bearer ").strip() if header else ""
        if not provided or not hmac.compare_digest(provided, self.token):
            await self.limiter.record_failure(client)
            logger.warning("operator_auth_failed", client=client, path=path)
            return JSONResponse({"detail": "invalid operator token"}, status_code=401)
        return None
