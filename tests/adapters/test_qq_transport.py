import asyncio
import json
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from platform_payloads import onebot_private_message
from websockets.asyncio.server import ServerConnection, serve

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.adapters.qq.transport import DeliveryError, QQTransport
from mybot.contracts import ChatKind, Platform, ReplyPlan


@dataclass
class RecordingPublisher:
    payloads: list[str] = field(default_factory=list)

    async def publish(self, payload: str) -> str:
        self.payloads.append(payload)
        return "1-0"


@dataclass
class ServerState:
    connections: int = 0
    auth_headers: list[str | None] = field(default_factory=list)
    received_actions: list[dict[str, object]] = field(default_factory=list)


def make_transport(port: int, publisher: RecordingPublisher) -> QQTransport:
    return QQTransport(
        ws_url=f"ws://127.0.0.1:{port}",
        access_token="qq-token",
        connection_id="qq-main",
        publisher=publisher,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.05,
    )


async def wait_for(predicate, wait_seconds: float = 3.0):  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")


@pytest.mark.asyncio
async def test_connects_with_token_translates_events_and_answers_actions() -> None:
    state = ServerState()
    publisher = RecordingPublisher()

    async def handler(connection: ServerConnection) -> None:
        state.connections += 1
        state.auth_headers.append(connection.request.headers.get("Authorization"))
        await connection.send(json.dumps(onebot_private_message()))
        await connection.send(json.dumps({"post_type": "meta_event", "time": 1}))
        async for raw in connection:
            action = json.loads(raw)
            state.received_actions.append(action)
            await connection.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 5001},
                        "echo": action["echo"],
                    }
                )
            )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = make_transport(port, publisher)
        task = asyncio.create_task(transport.run())
        try:
            await wait_for(lambda: len(publisher.payloads) == 1)
            event = InboundEvent.model_validate_json(publisher.payloads[0])
            assert event.envelope.platform is Platform.QQ
            assert event.envelope.id == "qq:qq-main:901"
            assert state.auth_headers == ["Bearer qq-token"]

            platform_id = await transport.send_reply(
                OutboundMessage(
                    internal_message_id=uuid4(),
                    platform=Platform.QQ,
                    connection_id="qq-main",
                    chat_kind=ChatKind.GROUP,
                    chat_id="333",
                    reply_plan=ReplyPlan(text_segments=("pong",)),
                    reply_to_platform_message_id="901",
                )
            )
            assert platform_id == "5001"
            action = state.received_actions[0]
            assert action["action"] == "send_msg"
            params = action["params"]
            assert params["message_type"] == "group"
            assert params["group_id"] == 333
            assert params["message"][0] == {"type": "reply", "data": {"id": "901"}}
            assert params["message"][1] == {"type": "text", "data": {"text": "pong"}}
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_reconnects_with_backoff_after_server_drop() -> None:
    state = ServerState()
    publisher = RecordingPublisher()

    async def dropping_handler(connection: ServerConnection) -> None:
        state.connections += 1
        await connection.close()

    async with serve(dropping_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = make_transport(port, publisher)
        task = asyncio.create_task(transport.run())
        try:
            await wait_for(lambda: state.connections >= 3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert state.connections >= 3


@pytest.mark.asyncio
async def test_failed_action_response_raises_delivery_error() -> None:
    async def failing_handler(connection: ServerConnection) -> None:
        async for raw in connection:
            action = json.loads(raw)
            await connection.send(
                json.dumps({"status": "failed", "retcode": 100, "echo": action["echo"]})
            )

    async with serve(failing_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = make_transport(port, RecordingPublisher())
        task = asyncio.create_task(transport.run())
        try:
            await wait_for(lambda: transport._connection is not None)
            with pytest.raises(DeliveryError):
                await transport.send_reply(
                    OutboundMessage(
                        internal_message_id=uuid4(),
                        platform=Platform.QQ,
                        connection_id="qq-main",
                        chat_kind=ChatKind.DIRECT,
                        chat_id="10001",
                        reply_plan=ReplyPlan(text_segments=("pong",)),
                    )
                )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_send_without_connection_raises_delivery_error() -> None:
    transport = make_transport(1, RecordingPublisher())

    with pytest.raises(DeliveryError):
        await transport.send_reply(
            OutboundMessage(
                internal_message_id=uuid4(),
                platform=Platform.QQ,
                connection_id="qq-main",
                chat_kind=ChatKind.DIRECT,
                chat_id="10001",
                reply_plan=ReplyPlan(text_segments=("pong",)),
            )
        )
