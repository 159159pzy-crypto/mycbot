"""FastAPI application factory and health endpoints."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mybot.infrastructure.correlation import CorrelationIdMiddleware
from mybot.infrastructure.health import ReadinessService, create_readiness_service
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

    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    async def readiness_endpoint() -> JSONResponse:
        ready, dependencies = await readiness_service.check()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "dependencies": dependencies,
            },
        )

    app.add_api_route("/health/live", liveness, methods=["GET"], tags=["health"])
    app.add_api_route("/health/ready", readiness_endpoint, methods=["GET"], tags=["health"])

    if bootstrap:
        configure_logging(resolved_settings.log_level)
        configure_telemetry(app, resolved_settings, service_name="mybot-api")
    return app


app = create_app()
