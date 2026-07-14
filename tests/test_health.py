import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from mybot.api import create_app
from mybot.infrastructure import health as health_module
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


@dataclass
class BlockingProbe:
    started: asyncio.Event

    async def check(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class CancellingProbe:
    async def check(self) -> None:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_default_readiness_factory_wraps_both_dependency_clients() -> None:
    service = create_readiness_service(Settings())

    try:
        assert isinstance(service.database, DatabaseProbe)
        assert isinstance(service.redis, RedisProbe)
    finally:
        await service.aclose()


def test_default_readiness_factory_configures_driver_timeouts(monkeypatch) -> None:
    captured_engine: dict[str, Any] = {}
    captured_redis: dict[str, Any] = {}

    class FakeEngine:
        pass

    class FakeRedisClient:
        pass

    class FakeRedisFactory:
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> FakeRedisClient:
            captured_redis.update({"url": url, **kwargs})
            return FakeRedisClient()

    def fake_create_async_engine(url: str, **kwargs: Any) -> FakeEngine:
        captured_engine.update({"url": url, **kwargs})
        return FakeEngine()

    monkeypatch.setattr(health_module, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(health_module, "Redis", FakeRedisFactory)

    service = create_readiness_service(
        Settings(
            database_connect_timeout_seconds=4,
            database_read_timeout_seconds=6,
            redis_connect_timeout_seconds=2.5,
            redis_read_timeout_seconds=3.5,
            health_probe_timeout_seconds=0.75,
        )
    )

    assert isinstance(service.database, DatabaseProbe)
    assert isinstance(service.redis, RedisProbe)
    assert service.probe_timeout_seconds == 0.75
    assert captured_engine["connect_args"] == {
        "connect_timeout": 4,
        "options": "-c statement_timeout=6000",
    }
    assert captured_redis["socket_connect_timeout"] == 2.5
    assert captured_redis["socket_timeout"] == 3.5


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


@pytest.mark.asyncio
async def test_readiness_times_out_a_blocking_probe_without_waiting_indefinitely() -> None:
    started = asyncio.Event()
    service = ReadinessService(
        database=BlockingProbe(started),
        redis=FakeProbe(True),
        probe_timeout_seconds=0.02,
    )

    began = monotonic()
    report = await service.check()
    elapsed = monotonic() - began

    assert started.is_set()
    assert elapsed < 0.2
    assert report.status == "not_ready"
    assert report.dependencies.database.status == "down"
    assert report.dependencies.database.detail == "TimeoutError"
    assert report.dependencies.redis.status == "up"


@pytest.mark.asyncio
async def test_readiness_propagates_probe_cancellation() -> None:
    service = ReadinessService(
        database=CancellingProbe(),
        redis=FakeProbe(True),
        probe_timeout_seconds=0.1,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.check()


def test_openapi_documents_typed_health_success_and_failure_contracts() -> None:
    app = create_app(
        readiness=ReadinessService(database=FakeProbe(True), redis=FakeProbe(True)),
        bootstrap=False,
    )

    paths = app.openapi()["paths"]
    live_schema = paths["/health/live"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    ready_responses = paths["/health/ready"]["get"]["responses"]
    ready_schema = ready_responses["200"]["content"]["application/json"]["schema"]
    unavailable_schema = ready_responses["503"]["content"]["application/json"]["schema"]

    assert live_schema["$ref"].endswith("/LivenessResponse")
    assert ready_schema["$ref"].endswith("/ReadinessResponse")
    assert unavailable_schema["$ref"] == ready_schema["$ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "request-500"])
async def test_unhandled_500_responses_keep_a_correlation_id(supplied: str | None) -> None:
    app = create_app(
        readiness=ReadinessService(database=FakeProbe(True), redis=FakeProbe(True)),
        bootstrap=False,
    )

    async def fail() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/failure", fail, methods=["GET"])
    headers = {} if supplied is None else {"X-Correlation-ID": supplied}
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/failure", headers=headers)

    assert response.status_code == 500
    correlation_id = response.headers["X-Correlation-ID"]
    if supplied is None:
        UUID(correlation_id)
    else:
        assert correlation_id == supplied
