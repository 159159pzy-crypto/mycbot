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
    from mybot.plugins.broker import PluginBroker, create_broker_router

    resolved_settings = settings or Settings()
    owns_readiness = readiness is None
    readiness_service = readiness or create_readiness_service(resolved_settings)
    plugin_broker = PluginBroker(
        grants=resolved_settings.plugin_grants(),
        invoke_timeout_seconds=resolved_settings.plugin_invoke_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        yield
        if owns_readiness:
            await readiness_service.aclose()

    from collections.abc import Awaitable, Callable

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    from mybot.operator.auth import AuthRateLimiter, OperatorAuth

    operator_auth = OperatorAuth(
        token=(
            resolved_settings.operator_token.get_secret_value()
            if resolved_settings.operator_token is not None
            else None
        ),
        limiter=AuthRateLimiter(
            max_failures=resolved_settings.operator_auth_max_failures,
            window_seconds=resolved_settings.operator_auth_window_seconds,
        ),
    )

    async def operator_auth_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        refusal = operator_auth.guard(request)
        if refusal is not None:
            return refusal
        return await call_next(request)

    app = FastAPI(title="mybot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BaseHTTPMiddleware, dispatch=operator_auth_middleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.state.plugin_broker = plugin_broker
    app.state.operator_auth = operator_auth
    app.include_router(create_broker_router(plugin_broker))

    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.streams import create_redis_backend
    from mybot.operator.api import OperatorContext, create_operator_router
    from mybot.repositories.audit import AuditRepository
    from mybot.repositories.operator_views import OperatorViews
    from mybot.repositories.system_kv import SystemKvRepository

    operator_sessions = create_session_factory(
        create_database_engine(resolved_settings)
    )
    app.include_router(
        create_operator_router(
            OperatorContext(
                views=OperatorViews(operator_sessions),
                audit=AuditRepository(operator_sessions),
                config=SystemKvRepository(operator_sessions),
                broker=plugin_broker,
                streams=create_redis_backend(
                    resolved_settings.redis_url.get_secret_value()
                ),
                settings=resolved_settings,
            )
        )
    )

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
