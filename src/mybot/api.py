"""FastAPI application factory and health endpoints."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
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
        service_quota_per_minute=resolved_settings.plugin_service_quota_per_minute,
    )
    operator_model_client = httpx.AsyncClient()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        await plugin_broker.restore()
        yield
        if owns_readiness:
            await readiness_service.aclose()
        await operator_model_client.aclose()
        await operator_backend.aclose()
        await plugin_broker.aclose()
        if hasattr(operator_auth.limiter, "aclose"):
            await operator_auth.limiter.aclose()  # type: ignore[attr-defined]

    from collections.abc import Awaitable, Callable

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    from mybot.operator.auth import AuthRateLimiter, OperatorAuth, create_redis_auth_limiter

    operator_auth = OperatorAuth(
        token=(
            resolved_settings.operator_token.get_secret_value()
            if resolved_settings.operator_token is not None
            else None
        ),
        limiter=(
            create_redis_auth_limiter(
                resolved_settings.redis_url.get_secret_value(),
                max_failures=resolved_settings.operator_auth_max_failures,
                window_seconds=resolved_settings.operator_auth_window_seconds,
            )
            if bootstrap
            else AuthRateLimiter(
                max_failures=resolved_settings.operator_auth_max_failures,
                window_seconds=resolved_settings.operator_auth_window_seconds,
            )
        ),
    )

    async def operator_auth_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        refusal = await operator_auth.guard(request)
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
    from mybot.infrastructure.model_routing import (
        MemoryModelCooldowns,
        ModelPurpose,
        ModelRouter,
        legacy_model_channels,
        openai_client_factory,
    )
    from mybot.infrastructure.streams import StreamPublisher, create_redis_backend
    from mybot.operator.api import (
        OperatorContext,
        collect_operator_metrics,
        create_operator_router,
        render_prometheus,
    )
    from mybot.repositories.audit import AuditRepository
    from mybot.repositories.knowledge import KnowledgeRepository
    from mybot.repositories.llm_calls import LlmCallLogRepository
    from mybot.repositories.memory import MemoryRepository
    from mybot.repositories.operator_views import OperatorViews
    from mybot.repositories.participation import WillingnessAuditRepository
    from mybot.repositories.profiles import ProfileRepository
    from mybot.repositories.safety import (
        EvaluationRepository,
        FeedbackRepository,
        ModerationAuditRepository,
        ToolApprovalRepository,
    )
    from mybot.repositories.system_kv import SystemKvRepository

    operator_sessions = create_session_factory(create_database_engine(resolved_settings))
    operator_backend = create_redis_backend(resolved_settings.redis_url.get_secret_value())
    from mybot.plugins.state import (
        MemoryPluginRegistrationStore,
        create_redis_plugin_registration_store,
    )

    plugin_broker.set_registration_store(
        create_redis_plugin_registration_store(
            resolved_settings.redis_url.get_secret_value(),
            ttl_seconds=resolved_settings.plugin_registration_ttl_seconds,
        )
        if bootstrap
        else MemoryPluginRegistrationStore()
    )
    operator_config = SystemKvRepository(operator_sessions)
    operator_attempts = LlmCallLogRepository(operator_sessions)
    operator_model_router = ModelRouter(
        config=operator_config,
        fallback_channels=legacy_model_channels(resolved_settings),
        cooldowns=MemoryModelCooldowns(),
        attempts=operator_attempts,
        client_factory=openai_client_factory(
            operator_model_client,
            temperature=resolved_settings.llm_temperature,
            max_output_tokens=resolved_settings.llm_max_output_tokens,
            timeout_seconds=resolved_settings.llm_timeout_seconds,
        ),
        secret_lookup=resolved_settings.model_secret,
        cache_ttl_seconds=resolved_settings.model_channels_cache_ttl_seconds,
        cooldown_seconds=resolved_settings.model_channel_cooldown_seconds,
    )
    operator_embeddings = operator_model_router.embeddings()
    operator_memory = MemoryRepository(operator_sessions)
    from mybot.evaluation import EvaluationCaseStore
    from mybot.plugins.control import PluginControlStore
    from mybot.plugins.services import plugin_services
    from mybot.plugins.tooling import PluginInstaller
    from mybot.repositories.conversations import ConversationRepository
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.pairing import PairingRepository
    from mybot.skills import SkillStore

    plugin_control = PluginControlStore(Path(resolved_settings.plugin_data_dir))

    plugin_broker.set_services(
        plugin_services(
            kv=operator_config,
            llm=operator_model_router.for_purpose(ModelPurpose.CHAT),
            memory=operator_memory,
            embeddings=operator_embeddings,
        )
    )
    operator_context = OperatorContext(
        views=OperatorViews(operator_sessions),
        audit=AuditRepository(operator_sessions),
        config=operator_config,
        broker=plugin_broker,
        streams=operator_backend,
        settings=resolved_settings,
        model_client=operator_model_client,
        model_attempts=operator_attempts,
        profiles=ProfileRepository(operator_sessions, legacy_persona=operator_config),
        memory=operator_memory,
        willingness=WillingnessAuditRepository(operator_sessions),
        knowledge=KnowledgeRepository(operator_sessions),
        embeddings=operator_embeddings,
        skills=SkillStore(Path(resolved_settings.skills_dir)),
        plugin_control=plugin_control,
        plugin_installer=PluginInstaller(
            registry_path=Path(resolved_settings.plugin_registry_path),
            store=plugin_control,
            client=operator_model_client,
            max_bytes=resolved_settings.plugin_install_max_bytes,
        ),
        pairing=PairingRepository(operator_sessions),
        moderation_audit=ModerationAuditRepository(operator_sessions),
        approvals=ToolApprovalRepository(operator_sessions),
        feedback=FeedbackRepository(operator_sessions),
        evaluations=EvaluationRepository(operator_sessions),
        evaluation_cases=EvaluationCaseStore(Path(resolved_settings.evaluation_cases_dir)),
        conversations=ConversationRepository(operator_sessions),
        messages=MessageRepository(operator_sessions),
        sandbox=StreamPublisher(
            backend=operator_backend,
            stream=resolved_settings.ingest_stream,
            maxlen=resolved_settings.stream_maxlen,
        ),
        outbound=StreamPublisher(
            backend=operator_backend,
            stream=resolved_settings.outbound_stream,
            maxlen=resolved_settings.stream_maxlen,
        ),
        knowledge_tasks=StreamPublisher(
            backend=operator_backend,
            stream=resolved_settings.knowledge_stream,
            maxlen=resolved_settings.stream_maxlen,
        ),
    )
    app.include_router(create_operator_router(operator_context))

    if resolved_settings.prometheus_enabled:

        async def prometheus_metrics() -> Response:
            values = await collect_operator_metrics(operator_context)
            return Response(
                render_prometheus(values),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        app.add_api_route("/metrics", prometheus_metrics, methods=["GET"], tags=["metrics"])

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
