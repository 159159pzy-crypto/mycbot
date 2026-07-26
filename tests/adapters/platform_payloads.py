"""Builders for realistic platform payload fixtures used by translator tests."""

from typing import Any


def onebot_private_message(
    *,
    message_id: int = 901,
    user_id: int = 10001,
    self_id: int = 10000,
    time: int = 1_784_000_000,
    message: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "self_id": self_id,
        "time": time,
        "message": message
        if message is not None
        else [{"type": "text", "data": {"text": "hello bot"}}],
        "raw_message": "hello bot",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": "alice"},
    }


def onebot_group_message(
    *,
    message_id: int = 902,
    user_id: int = 10001,
    group_id: int = 20002,
    self_id: int = 10000,
    time: int = 1_784_000_000,
    message: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = onebot_private_message(
        message_id=message_id,
        user_id=user_id,
        self_id=self_id,
        time=time,
        message=message,
    )
    payload["message_type"] = "group"
    payload["sub_type"] = "normal"
    payload["group_id"] = group_id
    return payload


def telegram_user(*, user_id: int = 777, is_bot: bool = False) -> dict[str, Any]:
    return {"id": user_id, "is_bot": is_bot, "first_name": "Alice", "username": "alice"}


def telegram_private_message(
    *,
    message_id: int = 55,
    user_id: int = 777,
    chat_id: int = 777,
    date: int = 1_784_000_000,
    text: str | None = "hello bot",
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "from": telegram_user(user_id=user_id),
        "chat": {"id": chat_id, "type": "private", "first_name": "Alice"},
        "date": date,
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return message


def telegram_group_message(
    *,
    message_id: int = 56,
    user_id: int = 777,
    chat_id: int = -100_1234,
    date: int = 1_784_000_000,
    text: str | None = "hello all",
    **extra: Any,
) -> dict[str, Any]:
    message = telegram_private_message(
        message_id=message_id,
        user_id=user_id,
        chat_id=chat_id,
        date=date,
        text=text,
        **extra,
    )
    message["chat"] = {"id": chat_id, "type": "supergroup", "title": "ops"}
    return message


def telegram_update(message: dict[str, Any], *, update_id: int = 1) -> dict[str, Any]:
    return {"update_id": update_id, "message": message}
