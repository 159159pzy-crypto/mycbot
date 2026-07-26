import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from platform_payloads import telegram_private_message, telegram_update

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.adapters.telegram.transport import TelegramTransport
from mybot.contracts import ChatKind, Platform, ReplyPlan


@dataclass
class RecordingPublisher:
    payloads: list[str] = field(default_factory=list)

    async def publish(self, payload: str) -> str:
        self.payloads.append(payload)
        return "1-0"


@dataclass
class FakeTelegramApi:
    updates_batches: list[list[dict[str, Any]]]
    fail_first_poll: bool = False
    seen_offsets: list[str | None] = field(default_factory=list)
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    chat_actions: list[dict[str, Any]] = field(default_factory=list)
    poll_calls: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getMe"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": 424242, "is_bot": True, "username": "mybot_bot"},
                },
            )
        if path.endswith("/getUpdates"):
            self.poll_calls += 1
            query = parse_qs(request.url.query.decode())
            self.seen_offsets.append(query.get("offset", [None])[0])
            if self.fail_first_poll and self.poll_calls == 1:
                return httpx.Response(500, text="internal error")
            batch = self.updates_batches.pop(0) if self.updates_batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/sendMessage"):
            body = json.loads(request.content.decode())
            self.sent_messages.append(body)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 9000 + len(self.sent_messages)}}
            )
        if path.endswith("/sendChatAction"):
            self.chat_actions.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Telegram API call: {path}")


def make_transport(api: FakeTelegramApi, publisher: RecordingPublisher) -> TelegramTransport:
    return TelegramTransport(
        token="tg-token",
        connection_id="telegram-main",
        publisher=publisher,
        client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        api_base_url="https://api.telegram.example",
        poll_timeout_seconds=0.05,
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
async def test_polls_translate_and_advance_offset() -> None:
    api = FakeTelegramApi(
        updates_batches=[
            [telegram_update(telegram_private_message(message_id=55), update_id=7)],
            [telegram_update(telegram_private_message(message_id=56), update_id=8)],
        ]
    )
    publisher = RecordingPublisher()
    transport = make_transport(api, publisher)

    task = asyncio.create_task(transport.run())
    try:
        await wait_for(lambda: len(publisher.payloads) >= 2 and len(api.seen_offsets) >= 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    first = InboundEvent.model_validate_json(publisher.payloads[0])
    assert first.envelope.platform is Platform.TELEGRAM
    assert first.envelope.id == "telegram:telegram-main:777:55"
    assert api.seen_offsets[0] is None
    assert "8" in api.seen_offsets
    assert "9" in api.seen_offsets


@pytest.mark.asyncio
async def test_poll_errors_back_off_and_preserve_offset() -> None:
    api = FakeTelegramApi(
        updates_batches=[
            [telegram_update(telegram_private_message(message_id=55), update_id=7)],
        ],
        fail_first_poll=True,
    )
    publisher = RecordingPublisher()
    transport = make_transport(api, publisher)

    task = asyncio.create_task(transport.run())
    try:
        await wait_for(lambda: len(publisher.payloads) >= 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert api.poll_calls >= 2
    assert api.seen_offsets[0] is None
    assert api.seen_offsets[1] is None


@pytest.mark.asyncio
async def test_send_reply_maps_reply_reference_and_returns_message_id() -> None:
    api = FakeTelegramApi(updates_batches=[])
    transport = make_transport(api, RecordingPublisher())

    platform_id = await transport.send_reply(
        OutboundMessage(
            internal_message_id=uuid4(),
            platform=Platform.TELEGRAM,
            connection_id="telegram-main",
            chat_kind=ChatKind.GROUP,
            chat_id="-1001234",
            reply_plan=ReplyPlan(text_segments=("first", "second")),
            reply_to_platform_message_id="55",
        )
    )

    assert platform_id == "9002"
    assert len(api.sent_messages) == 2
    assert api.sent_messages[0]["chat_id"] == "-1001234"
    assert api.sent_messages[0]["text"] == "first"
    assert api.sent_messages[0]["reply_to_message_id"] == "55"
    assert "reply_to_message_id" not in api.sent_messages[1]


@pytest.mark.asyncio
async def test_send_typing_posts_chat_action() -> None:
    api = FakeTelegramApi(updates_batches=[])
    transport = make_transport(api, RecordingPublisher())

    await transport.send_typing("777")

    assert api.chat_actions == [{"chat_id": "777", "action": "typing"}]
