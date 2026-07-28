from collections.abc import AsyncIterator

import httpx
import pytest

from mybot.api import create_app
from mybot.infrastructure.health import ReadinessService
from mybot.operator.auth import AuthRateLimiter, OperatorAuth
from mybot.settings import Settings


class AlwaysUpProbe:
    async def check(self) -> None:
        return None


def make_app(*, token: str | None = "s3cret", max_failures: int = 10):  # type: ignore[no-untyped-def]
    settings = Settings(
        operator_token=token,
        operator_auth_max_failures=max_failures,
    )
    return create_app(
        settings=settings,
        readiness=ReadinessService(database=AlwaysUpProbe(), redis=AlwaysUpProbe()),
        bootstrap=False,
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("1.2.3.4", 5000)),
        base_url="http://api",
    ) as instance:
        yield instance


@pytest.mark.asyncio
async def test_health_and_broker_surfaces_stay_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/plugin-broker/tools")).status_code == 200


@pytest.mark.asyncio
async def test_operator_requires_a_valid_bearer_token(client: httpx.AsyncClient) -> None:
    missing = await client.get("/operator/ping")
    assert missing.status_code == 401
    wrong = await client.get(
        "/operator/ping", headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status_code == 401
    right = await client.get(
        "/operator/ping", headers={"Authorization": "Bearer s3cret"}
    )
    assert right.status_code == 200
    assert right.json() == {"ok": True}


@pytest.mark.asyncio
async def test_unset_token_fails_closed() -> None:
    app = make_app(token=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("1.2.3.4", 5000)),
        base_url="http://api",
    ) as client:
        response = await client.get(
            "/operator/ping", headers={"Authorization": "Bearer anything"}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_repeated_failures_are_rate_limited() -> None:
    app = make_app(max_failures=3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("9.9.9.9", 5000)),
        base_url="http://api",
    ) as client:
        for _ in range(3):
            assert (
                await client.get(
                    "/operator/x", headers={"Authorization": "Bearer wrong"}
                )
            ).status_code == 401
        blocked = await client.get(
            "/operator/x", headers={"Authorization": "Bearer s3cret"}
        )
    assert blocked.status_code == 429


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_rate_limiter_failures_expire_with_the_clock() -> None:
    clock = Clock()
    limiter = AuthRateLimiter(max_failures=2, window_seconds=60.0, clock=clock)

    await limiter.record_failure("client")
    await limiter.record_failure("client")
    assert await limiter.blocked("client") is True

    clock.now += 61.0
    assert await limiter.blocked("client") is False


@pytest.mark.asyncio
async def test_guard_open_paths_never_touch_the_limiter() -> None:
    from typing import ClassVar

    limiter = AuthRateLimiter(max_failures=1)
    auth = OperatorAuth(token=None, limiter=limiter)

    class FakeRequest:
        class _URL:
            path = "/health/ready"

        url = _URL()
        headers: ClassVar[dict[str, str]] = {}
        client = None

    assert await auth.guard(FakeRequest()) is None  # type: ignore[arg-type]
