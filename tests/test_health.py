from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient

from mybot.api import create_app
from mybot.infrastructure.health import (
    DatabaseProbe,
    ReadinessService,
    RedisProbe,
    create_readiness_service,
)
from mybot.settings import Settings


@dataclass
class FakeProbe:
    available: bool
    detail: str = "dependency unavailable"
    calls: int = 0

    async def check(self) -> None:
        self.calls += 1
        if not self.available:
            raise ConnectionError(self.detail)


@pytest.mark.asyncio
async def test_default_readiness_factory_wraps_both_dependency_clients() -> None:
    service = create_readiness_service(Settings())

    try:
        assert isinstance(service.database, DatabaseProbe)
        assert isinstance(service.redis, RedisProbe)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_liveness_is_unconditional() -> None:
    database = FakeProbe(False)
    redis = FakeProbe(False)
    app = create_app(readiness=ReadinessService(database=database, redis=redis), bootstrap=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert database.calls == 0
    assert redis.calls == 0


@pytest.mark.asyncio
async def test_readiness_reports_each_healthy_dependency() -> None:
    database = FakeProbe(True)
    redis = FakeProbe(True)
    app = create_app(readiness=ReadinessService(database=database, redis=redis), bootstrap=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready", headers={"X-Correlation-ID": "request-42"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {
            "database": {"status": "up"},
            "redis": {"status": "up"},
        },
    }
    assert response.headers["X-Correlation-ID"] == "request-42"
    assert database.calls == 1
    assert redis.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_ok", "redis_ok", "failed_name"),
    [(False, True, "database"), (True, False, "redis"), (False, False, "database")],
)
async def test_readiness_returns_503_and_keeps_dependency_failures_separate(
    database_ok: bool, redis_ok: bool, failed_name: str
) -> None:
    app = create_app(
        readiness=ReadinessService(
            database=FakeProbe(database_ok, "db offline"),
            redis=FakeProbe(redis_ok, "cache offline"),
        ),
        bootstrap=False,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")

    payload = response.json()
    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["dependencies"][failed_name]["status"] == "down"
    assert payload["dependencies"]["database"]["status"] == (
        "up" if database_ok else "down"
    )
    assert payload["dependencies"]["redis"]["status"] == "up" if redis_ok else "down"
    assert response.headers["X-Correlation-ID"]
