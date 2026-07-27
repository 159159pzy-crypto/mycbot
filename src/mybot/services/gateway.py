"""The gateway lifecycle: platform connections in, reply delivery out."""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import UUID

import httpx
import structlog
from pydantic import JsonValue

from mybot.adapters import OutboundMessage
from mybot.contracts import Platform, ReplyPlan
from mybot.infrastructure.streams import (
    StreamConsumer,
    StreamPublisher,
    create_redis_backend,
)
from mybot.repositories.traces import TraceStatus
from mybot.runtime import ProcessMode
from mybot.settings import Settings

logger = structlog.get_logger("mybot.gateway")

_MAX_TYPING_DELAY_SECONDS = 8.0


class PlatformSender(Protocol):
    async def run(self) -> None: ...

    async def send_reply(self, message: OutboundMessage) -> str: ...

    async def send_typing(self, chat_id: str) -> None: ...


class DeliveryStore(Protocol):
    async def mark_delivered(self, message_id: UUID, platform_message_id: str) -> None: ...


class TraceSink(Protocol):
    async def record(
        self,
        *,
        trace_id: str,
        stage: str,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> UUID: ...


@dataclass(slots=True)
class SandboxSender:
    """No-network platform sender; persistence is the sandbox transcript transport."""

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def send_reply(self, message: OutboundMessage) -> str:
        return f"sandbox:{message.internal_message_id}"

    async def send_typing(self, chat_id: str) -> None:
        return None


def typing_delay_seconds(plan: ReplyPlan) -> float:
    """Simulated typing time for a plan, bounded so delivery stays prompt."""

    if not plan.typing.enabled:
        return 0.0
    total_characters = sum(len(text) for text in plan.text_segments)
    delay = (
        plan.typing.initial_delay_ms / 1000.0
        + total_characters / plan.typing.chars_per_second
    )
    return min(delay, _MAX_TYPING_DELAY_SECONDS)


@dataclass(slots=True)
class GatewayService:
    """LifecycleService owning platform adapters and outbound delivery."""

    qq: PlatformSender | None
    telegram: PlatformSender | None
    outbound_consumer: StreamConsumer
    deliveries: DeliveryStore
    sandbox: PlatformSender | None = None
    traces: TraceSink | None = None

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger.info("gateway_started", mode=mode.value)
        tasks: list[asyncio.Task[None]] = []
        for platform, adapter in (
            ("qq", self.qq),
            ("telegram", self.telegram),
            ("sandbox", self.sandbox),
        ):
            if adapter is None:
                logger.info("gateway_platform_disabled", platform=platform)
                continue
            tasks.append(asyncio.create_task(adapter.run(), name=f"gateway-{platform}"))
        tasks.append(
            asyncio.create_task(
                self.outbound_consumer.run(self.deliver_payload, stop_event=stop_event),
                name="gateway-outbound",
            )
        )
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("gateway_stopped", mode=mode.value)

    async def deliver_payload(self, payload: str) -> None:
        message = OutboundMessage.model_validate_json(payload)
        started = monotonic()
        if message.platform is Platform.QQ:
            adapter = self.qq
        elif message.platform is Platform.TELEGRAM:
            adapter = self.telegram
        else:
            adapter = self.sandbox
        if adapter is None:
            await self._trace(
                message,
                status="error",
                duration_ms=int((monotonic() - started) * 1_000),
                attributes={"error_code": "adapter_missing"},
            )
            raise RuntimeError(
                f"no adapter configured for platform {message.platform.value}"
            )
        delay = typing_delay_seconds(message.reply_plan)
        if delay > 0:
            await adapter.send_typing(message.chat_id)
            await asyncio.sleep(delay)
        try:
            platform_message_id = await adapter.send_reply(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._trace(
                message,
                status="error",
                duration_ms=int((monotonic() - started) * 1_000),
                attributes={"error_code": type(error).__name__},
            )
            raise
        await self.deliveries.mark_delivered(message.internal_message_id, platform_message_id)
        await self._trace(
            message,
            duration_ms=int((monotonic() - started) * 1_000),
            attributes={"platform": message.platform.value},
        )
        logger.info(
            "reply_delivered",
            platform=message.platform.value,
            chat_id=message.chat_id,
            platform_message_id=platform_message_id,
        )

    async def _trace(
        self,
        message: OutboundMessage,
        *,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self.traces is None:
            return
        try:
            await self.traces.record(
                trace_id=message.trace_id,
                stage="outbound.delivery",
                status=status,
                duration_ms=duration_ms,
                message_id=message.internal_message_id,
                attributes=attributes,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("trace_span_record_failed", stage="outbound.delivery")


def create_gateway_service(settings: Settings) -> GatewayService:
    """Build the production gateway from settings-backed adapters and streams."""

    from mybot.adapters.qq.transport import QQTransport
    from mybot.adapters.telegram.transport import TelegramTransport
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.traces import TraceSpanRepository

    backend = create_redis_backend(settings.redis_url.get_secret_value())
    ingest_publisher = StreamPublisher(
        backend=backend, stream=settings.ingest_stream, maxlen=settings.stream_maxlen
    )
    qq: QQTransport | None = None
    if settings.napcat_ws_url is not None:
        qq = QQTransport(
            ws_url=settings.napcat_ws_url.get_secret_value(),
            access_token=(
                settings.qq_access_token.get_secret_value()
                if settings.qq_access_token is not None
                else None
            ),
            connection_id=settings.qq_connection_id,
            publisher=ingest_publisher,
            reconnect_initial_seconds=settings.gateway_reconnect_initial_seconds,
            reconnect_max_seconds=settings.gateway_reconnect_max_seconds,
        )
    telegram: TelegramTransport | None = None
    if settings.telegram_bot_token is not None:
        telegram = TelegramTransport(
            token=settings.telegram_bot_token.get_secret_value(),
            connection_id=settings.telegram_connection_id,
            publisher=ingest_publisher,
            client=httpx.AsyncClient(),
            api_base_url=settings.telegram_api_base_url,
            poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
            reconnect_initial_seconds=settings.gateway_reconnect_initial_seconds,
            reconnect_max_seconds=settings.gateway_reconnect_max_seconds,
        )
    consumer = StreamConsumer(
        backend,
        stream=settings.outbound_stream,
        group=settings.outbound_group,
        consumer=f"gateway-{os.getpid()}",
        dead_letter_stream=f"{settings.outbound_stream}:dead",
        max_attempts=settings.stream_delivery_max_attempts,
        dedupe_ttl_seconds=settings.stream_dedupe_ttl_seconds,
        dedupe_prefix="mybot:seen:outbound",
        block_ms=settings.stream_block_ms,
        claim_min_idle_ms=settings.stream_claim_min_idle_ms,
    )
    sessions = create_session_factory(create_database_engine(settings))
    return GatewayService(
        qq=qq,
        telegram=telegram,
        outbound_consumer=consumer,
        deliveries=MessageRepository(sessions),
        sandbox=SandboxSender(),
        traces=TraceSpanRepository(sessions),
    )
