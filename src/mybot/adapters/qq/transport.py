"""NapCat (OneBot v11) WebSocket transport: inbound events and send actions."""

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from mybot.adapters import OutboundMessage
from mybot.adapters.payload import (
    RawMapping,
    as_string_mapping,
    get_int,
    get_mapping,
    get_str,
)
from mybot.adapters.qq.translate import translate_qq_event
from mybot.contracts import ChatKind

logger = structlog.get_logger("mybot.gateway.qq")

_ACTION_TIMEOUT_SECONDS = 10.0


class DeliveryError(RuntimeError):
    """A platform rejected or failed an outbound send action."""


class Publisher(Protocol):
    async def publish(self, payload: str) -> str: ...


@dataclass
class QQTransport:
    """Owns the NapCat WebSocket connection for one QQ bot account."""

    ws_url: str
    access_token: str | None
    connection_id: str
    publisher: Publisher
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    _connection: ClientConnection | None = field(default=None, init=False, repr=False)
    _pending: dict[str, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict[str, "asyncio.Future[dict[str, object]]"],
        init=False,
        repr=False,
    )

    async def run(self) -> None:
        """Maintain the connection forever; cancellation is the only exit."""

        backoff = self.reconnect_initial_seconds
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, additional_headers=headers
                ) as connection:
                    logger.info("qq_connected", connection_id=self.connection_id)
                    self._connection = connection
                    backoff = self.reconnect_initial_seconds
                    async for raw in connection:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "qq_connection_lost",
                    connection_id=self.connection_id,
                    error=type(error).__name__,
                )
            finally:
                self._connection = None
                self._fail_pending("connection lost")
            delay = backoff * (0.8 + 0.4 * random.random())
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self.reconnect_max_seconds)

    async def send_reply(self, message: OutboundMessage) -> str:
        """Send every reply segment as a message; return the last platform message id."""

        last_message_id = ""
        for index, text in enumerate(message.reply_plan.text_segments):
            segments: list[dict[str, object]] = []
            if index == 0 and message.reply_to_platform_message_id is not None:
                segments.append(
                    {"type": "reply", "data": {"id": message.reply_to_platform_message_id}}
                )
            segments.append({"type": "text", "data": {"text": text}})
            params: dict[str, object] = {"message": segments}
            if message.chat_kind is ChatKind.DIRECT:
                params["message_type"] = "private"
                params["user_id"] = _as_onebot_id(message.chat_id)
            else:
                params["message_type"] = "group"
                params["group_id"] = _as_onebot_id(message.chat_id)
            data = await self._call("send_msg", params)
            message_id = get_int(data, "message_id")
            if message_id is None:
                raise DeliveryError("send_msg response did not include a message_id")
            last_message_id = str(message_id)
        return last_message_id

    async def send_typing(self, chat_id: str) -> None:
        """OneBot v11 has no typing indicator; deliberately a no-op."""

    async def _call(self, action: str, params: dict[str, object]) -> RawMapping:
        connection = self._connection
        if connection is None:
            raise DeliveryError("QQ connection is not established")
        echo = str(uuid4())
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await connection.send(
                json.dumps({"action": action, "params": params, "echo": echo})
            )
            async with asyncio.timeout(_ACTION_TIMEOUT_SECONDS):
                response = await future
        except TimeoutError as error:
            raise DeliveryError(f"{action} timed out") from error
        finally:
            self._pending.pop(echo, None)
        status = get_str(response, "status")
        retcode = get_int(response, "retcode")
        if status not in {"ok", "async"} or (retcode not in {0, 1, None}):
            raise DeliveryError(f"{action} failed with status={status} retcode={retcode}")
        return get_mapping(response, "data") or {}

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            decoded = json.loads(raw)
        except ValueError:
            logger.warning("qq_frame_not_json", connection_id=self.connection_id)
            return
        frame = as_string_mapping(decoded)
        if frame is None:
            return
        echo = get_str(frame, "echo")
        if echo is not None:
            future = self._pending.get(echo)
            if future is not None and not future.done():
                future.set_result(dict(frame))
            return
        self_id = get_int(frame, "self_id") or 0
        try:
            event = translate_qq_event(
                frame, connection_id=self.connection_id, self_id=self_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("qq_event_translation_failed", connection_id=self.connection_id)
            return
        if event is None:
            return
        await self.publisher.publish(event.model_dump_json())

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeliveryError(reason))
        self._pending.clear()


def _as_onebot_id(chat_id: str) -> int | str:
    try:
        return int(chat_id)
    except ValueError:
        return chat_id
