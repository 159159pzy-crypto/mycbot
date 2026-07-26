"""Bearer authentication for operator APIs with failure rate limiting."""

import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic

import structlog
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

    def blocked(self, client: str) -> bool:
        return len(self._bucket(client)) >= self.max_failures

    def record_failure(self, client: str) -> None:
        self._bucket(client).append(self.clock())


@dataclass(slots=True)
class OperatorAuth:
    """Policy: fail closed with no token; health and broker surfaces stay open."""

    token: str | None
    limiter: AuthRateLimiter

    def guard(self, request: Request) -> Response | None:
        """Return a refusal response, or None to let the request through."""

        path = request.url.path
        if not (path == _GUARDED_PREFIX or path.startswith(f"{_GUARDED_PREFIX}/")):
            return None
        client = request.client.host if request.client else "unknown"
        if self.limiter.blocked(client):
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
            self.limiter.record_failure(client)
            logger.warning("operator_auth_failed", client=client, path=path)
            return JSONResponse({"detail": "invalid operator token"}, status_code=401)
        return None
