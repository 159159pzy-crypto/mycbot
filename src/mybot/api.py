"""FastAPI application factory and health endpoints."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from mybot.infrastructure.correlation import CorrelationIdMiddleware
from mybot.infrastructure.health import (
    LivenessResponse,
    ReadinessResponse,
    ReadinessService,
    create_readiness_service,
)
from mybot.infrastructure.logging import configure_logging
from mybot.infrastructure.telemetry import configure_telemetry
from mybot.settings import Settings


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
    bootstrap: bool = True,
) -> FastAPI:
    resolved_settings = settings or Settings()
    owns_readiness = readiness is None
    readiness_service = readiness or create_readiness_service(resolved_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        yield
        if owns_readiness:
            await readiness_service.aclose()

    app = FastAPI(title="mybot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)

    async def liveness() -> LivenessResponse:
        return LivenessResponse()

    async def readiness_endpoint(response: Response) -> ReadinessResponse:
        report = await readiness_service.check()
        if report.status == "not_ready":
            response.status_code = 503
        return report

    app.add_api_route(
        "/health/live",
        liveness,
        methods=["GET"],
        tags=["health"],
        response_model=LivenessResponse,
    )
    app.add_api_route(
        "/health/ready",
        readiness_endpoint,
        methods=["GET"],
        tags=["health"],
        response_model=ReadinessResponse,
        response_model_exclude_none=True,
        responses={503: {"model": ReadinessResponse, "description": "Dependency unavailable"}},
    )

    if bootstrap:
        configure_logging(resolved_settings.log_level)
        configure_telemetry(app, resolved_settings, service_name="mybot-api")
    return app


app = create_app()
