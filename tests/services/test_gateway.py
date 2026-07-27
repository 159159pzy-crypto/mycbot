import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from mybot.adapters import OutboundMessage
from mybot.contracts import ChatKind, Platform, ReplyPlan, TypingProfile
from mybot.infrastructure.streams import (
    MemoryStreamBackend,
    StreamConsumer,
    StreamPublisher,
)
from mybot.runtime import ProcessMode
from mybot.services.gateway import GatewayService, SandboxSender, typing_delay_seconds


@dataclass
class FakeAdapter:
    replies: list[OutboundMessage] = field(default_factory=list)
    typing_chats: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def send_reply(self, message: OutboundMessage) -> str:
        self.replies.append(message)
        return "7001"

    async def send_typing(self, chat_id: str) -> None:
        self.typing_chats.append(chat_id)


@dataclass
class FakeDeliveries:
    delivered: list[tuple[UUID, str]] = field(default_factory=list)

    async def mark_delivered(self, message_id: UUID, platform_message_id: str) -> None:
        self.delivered.append((message_id, platform_message_id))


def outbound(
    *,
    platform: Platform = Platform.QQ,
    typing: bool = False,
) -> OutboundMessage:
    return OutboundMessage(
        internal_message_id=uuid4(),
        platform=platform,
        connection_id="qq-main" if platform is Platform.QQ else "telegram-main",
        chat_kind=ChatKind.DIRECT,
        chat_id="10001",
        reply_plan=ReplyPlan(
            text_segments=("pong",),
            typing=TypingProfile(
                enabled=typing, initial_delay_ms=0, chars_per_second=100.0
            ),
        ),
    )


def make_service(
    backend: MemoryStreamBackend,
    *,
    qq: FakeAdapter | None,
    telegram: FakeAdapter | None,
) -> tuple[GatewayService, FakeDeliveries]:
    deliveries = FakeDeliveries()
    consumer = StreamConsumer(
        backend,
        stream="mybot:outbound",
        group="gateway",
        consumer="gateway-test",
        dead_letter_stream="mybot:outbound:dead",
        max_attempts=3,
        dedupe_ttl_seconds=60,
        dedupe_prefix="mybot:seen:outbound",
        block_ms=10,
        claim_min_idle_ms=5_000,
    )
    return (
        GatewayService(qq=qq, telegram=telegram, outbound_consumer=consumer, deliveries=deliveries),
        deliveries,
    )


@pytest.mark.asyncio
async def test_outbound_routes_to_matching_platform_and_marks_delivered() -> None:
    backend = MemoryStreamBackend()
    qq = FakeAdapter()
    telegram = FakeAdapter()
    service, deliveries = make_service(backend, qq=qq, telegram=telegram)
    publisher = StreamPublisher(backend=backend, stream="mybot:outbound", maxlen=100)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.GATEWAY, stop_event))
    qq_message = outbound(platform=Platform.QQ)
    telegram_message = outbound(platform=Platform.TELEGRAM)
    await publisher.publish(qq_message.model_dump_json())
    await publisher.publish(telegram_message.model_dump_json())
    for _ in range(300):
        if len(deliveries.delivered) == 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert [message.platform for message in qq.replies] == [Platform.QQ]
    assert [message.platform for message in telegram.replies] == [Platform.TELEGRAM]
    assert sorted(delivered_id for delivered_id, _ in deliveries.delivered) == sorted(
        [qq_message.internal_message_id, telegram_message.internal_message_id]
    )
    assert {platform_id for _, platform_id in deliveries.delivered} == {"7001"}


@pytest.mark.asyncio
async def test_unconfigured_platform_still_runs_the_rest() -> None:
    backend = MemoryStreamBackend()
    telegram = FakeAdapter()
    service, deliveries = make_service(backend, qq=None, telegram=telegram)
    publisher = StreamPublisher(backend=backend, stream="mybot:outbound", maxlen=100)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.GATEWAY, stop_event))
    await publisher.publish(outbound(platform=Platform.TELEGRAM).model_dump_json())
    for _ in range(300):
        if deliveries.delivered:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert telegram.started.is_set()
    assert len(deliveries.delivered) == 1


@pytest.mark.asyncio
async def test_typing_simulation_fires_before_sending_when_enabled() -> None:
    backend = MemoryStreamBackend()
    qq = FakeAdapter()
    service, _ = make_service(backend, qq=qq, telegram=None)

    await service.deliver_payload(outbound(typing=True).model_dump_json())
    await service.deliver_payload(outbound(typing=False).model_dump_json())

    assert qq.typing_chats == ["10001"]
    assert len(qq.replies) == 2


def test_typing_delay_is_bounded_and_zero_when_disabled() -> None:
    silent = ReplyPlan(text_segments=("hello",))
    chatty = ReplyPlan(
        text_segments=("x" * 3000,),
        typing=TypingProfile(enabled=True, initial_delay_ms=30_000, chars_per_second=1.0),
    )
    quick = ReplyPlan(
        text_segments=("hi",),
        typing=TypingProfile(enabled=True, initial_delay_ms=100, chars_per_second=20.0),
    )

    assert typing_delay_seconds(silent) == 0.0
    assert typing_delay_seconds(chatty) == 8.0
    assert 0.0 < typing_delay_seconds(quick) < 1.0


@pytest.mark.asyncio
async def test_delivery_to_missing_adapter_raises_for_redelivery() -> None:
    backend = MemoryStreamBackend()
    service, deliveries = make_service(backend, qq=None, telegram=None)

    with pytest.raises(RuntimeError):
        await service.deliver_payload(outbound(platform=Platform.QQ).model_dump_json())
    assert deliveries.delivered == []


@pytest.mark.asyncio
async def test_sandbox_sender_completes_delivery_without_external_platform() -> None:
    backend = MemoryStreamBackend()
    service, deliveries = make_service(backend, qq=None, telegram=None)
    service.sandbox = SandboxSender()
    message = outbound(platform=Platform.SANDBOX)

    await service.deliver_payload(message.model_dump_json())

    assert deliveries.delivered == [
        (message.internal_message_id, f"sandbox:{message.internal_message_id}")
    ]
