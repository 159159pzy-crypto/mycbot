"""Telegram Bot API transport: long polling inbound and message sending."""

import asyncio
from dataclasses import dataclass, field
from typing import Protocol, cast

import httpx
import structlog

from mybot.adapters import OutboundMessage
from mybot.adapters.payload import RawMapping, as_string_mapping, get_id, get_int, get_str
from mybot.adapters.telegram.translate import (
    TELEGRAM_CAPABILITIES,
    encode_telegram_reply,
    translate_telegram_update,
)

logger = structlog.get_logger("mybot.gateway.telegram")


class DeliveryError(RuntimeError):
    """Telegram rejected or failed an outbound send."""


class Publisher(Protocol):
    async def publish(self, payload: str) -> str: ...


@dataclass
class TelegramTransport:
    """Owns long polling and sends for one Telegram bot token."""

    token: str
    connection_id: str
    publisher: Publisher
    client: httpx.AsyncClient
    api_base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: float = 50.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    _bot_id: int = field(default=0, init=False)
    _bot_username: str = field(default="", init=False)

    async def run(self) -> None:
        """Poll updates forever; cancellation is the only exit."""

        await self._discover_identity()
        offset: int | None = None
        backoff = self.reconnect_initial_seconds
        while True:
            try:
                params: dict[str, str | int] = {
                    "timeout": int(self.poll_timeout_seconds),
                    "allowed_updates": '["message"]',
                }
                if offset is not None:
                    params["offset"] = offset
                payload = await self._request("getUpdates", params)
                backoff = self.reconnect_initial_seconds
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "telegram_poll_failed",
                    connection_id=self.connection_id,
                    error=type(error).__name__,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)
                continue
            for raw_update in payload:
                update = as_string_mapping(raw_update)
                if update is None:
                    continue
                update_id = get_int(update, "update_id")
                if update_id is not None:
                    offset = update_id + 1
                await self._publish_update(update)
            # Guarantee fairness even when the HTTP transport completes synchronously.
            await asyncio.sleep(0)

    async def send_reply(self, message: OutboundMessage) -> str:
        """Send each reply segment; return the last Telegram message id."""

        last_message_id = ""
        actions = encode_telegram_reply(
            message.reply_plan,
            capabilities=TELEGRAM_CAPABILITIES,
            reply_to_message_id=message.reply_to_platform_message_id,
        )
        for action in actions:
            method = str(action["method"])
            body = dict(cast(dict[str, object], action["body"]))
            body["chat_id"] = message.chat_id
            sent = await self._post(method, body)
            sent_mapping = as_string_mapping(sent) or {}
            message_id = get_id(sent_mapping, "message_id")
            if message_id is None:
                raise DeliveryError(f"{method} response did not include a message_id")
            last_message_id = message_id
        return last_message_id

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self._post("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("telegram_typing_failed", connection_id=self.connection_id)

    async def _discover_identity(self) -> None:
        backoff = self.reconnect_initial_seconds
        while True:
            try:
                me = as_string_mapping(await self._post("getMe", {})) or {}
                bot_id = get_int(me, "id")
                username = get_str(me, "username")
                if bot_id is None or username is None:
                    raise DeliveryError("getMe response was incomplete")
                self._bot_id = bot_id
                self._bot_username = username
                logger.info(
                    "telegram_identity_discovered",
                    connection_id=self.connection_id,
                    username=username,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "telegram_identity_discovery_failed",
                    connection_id=self.connection_id,
                    error=type(error).__name__,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)

    async def _publish_update(self, update: RawMapping) -> None:
        try:
            event = translate_telegram_update(
                update,
                connection_id=self.connection_id,
                self_id=self._bot_id,
                bot_username=self._bot_username,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram_update_translation_failed", connection_id=self.connection_id)
            return
        if event is None:
            return
        await self.publisher.publish(event.model_dump_json())

    async def _request(self, method: str, params: dict[str, str | int]) -> list[object]:
        response = await self.client.get(
            f"{self.api_base_url}/bot{self.token}/{method}",
            params=params,
            timeout=self.poll_timeout_seconds + 10.0,
        )
        response.raise_for_status()
        body = as_string_mapping(response.json()) or {}
        if body.get("ok") is not True:
            raise DeliveryError(f"{method} returned ok=false")
        result = body.get("result")
        if isinstance(result, list):
            return list(cast(list[object], result))
        return []

    async def _post(self, method: str, body: dict[str, object]) -> object:
        response = await self.client.post(
            f"{self.api_base_url}/bot{self.token}/{method}",
            json=body,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = as_string_mapping(response.json()) or {}
        if payload.get("ok") is not True:
            raise DeliveryError(f"{method} returned ok=false")
        return payload.get("result")
