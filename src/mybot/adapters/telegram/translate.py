"""Pure translation from Telegram Bot API updates to public message contracts."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from mybot.adapters import InboundEvent
from mybot.adapters.identity import telegram_envelope_id, telegram_identity
from mybot.adapters.payload import (
    RawMapping,
    as_json_value,
    as_string_mapping,
    get_id,
    get_int,
    get_mapping,
    get_sequence,
    get_str,
)
from mybot.contracts import (
    AtSegment,
    ChatKind,
    FileSegment,
    ImageSegment,
    MessageEnvelope,
    Platform,
    PlatformCapabilities,
    ReplyPlan,
    StickerSegment,
    TextSegment,
    VoiceSegment,
)
from mybot.contracts.json import FrozenJsonValue

TELEGRAM_CAPABILITIES = PlatformCapabilities(
    editing=True,
    replies=True,
    typing=True,
    proactive_messages=True,
    combined_media_text=True,
    threads=True,
    reactions=False,
    images=True,
    mentions=True,
    stickers=True,
    voice_messages=True,
)

_CHAT_KINDS: dict[str, ChatKind] = {
    "private": ChatKind.DIRECT,
    "group": ChatKind.GROUP,
    "supergroup": ChatKind.GROUP,
    "channel": ChatKind.CHANNEL,
}

_UNSUPPORTED_CONTENT_KEYS = (
    "audio",
    "video",
    "video_note",
    "animation",
    "location",
    "venue",
    "contact",
    "poll",
    "dice",
)

type ContentSegment = (
    TextSegment | ImageSegment | FileSegment | AtSegment | StickerSegment | VoiceSegment
)


def translate_telegram_update(
    update: Mapping[str, object],
    *,
    connection_id: str,
    self_id: int,
    bot_username: str,
) -> InboundEvent | None:
    """Translate one Bot API update; non-message or contentless updates yield None."""

    message = get_mapping(update, "message")
    if message is None:
        return None
    chat = get_mapping(message, "chat")
    sender = get_mapping(message, "from")
    if chat is None or sender is None:
        return None
    chat_kind = _CHAT_KINDS.get(get_str(chat, "type") or "")
    chat_id = get_id(chat, "id")
    sender_id = get_id(sender, "id")
    message_id = get_id(message, "message_id")
    occurred = get_int(message, "date")
    if None in (chat_kind, chat_id, sender_id, message_id, occurred):
        return None
    assert chat_kind is not None and chat_id is not None
    assert sender_id is not None and message_id is not None and occurred is not None

    segments: list[ContentSegment] = []
    text = (get_str(message, "text") or "").strip()
    if text:
        segments.append(TextSegment(text=text))
    caption = (get_str(message, "caption") or "").strip()
    if caption:
        segments.append(TextSegment(text=caption))

    photo_sizes = [
        size
        for size in (as_string_mapping(raw) for raw in get_sequence(message, "photo"))
        if size is not None
    ]
    if photo_sizes:
        best = max(photo_sizes, key=_photo_area)
        file_id = get_str(best, "file_id")
        if file_id:
            segments.append(ImageSegment(url=f"tg-file://{file_id}"))

    document = get_mapping(message, "document")
    if document is not None:
        file_id = get_str(document, "file_id")
        if file_id:
            segments.append(
                FileSegment(
                    name=get_str(document, "file_name") or "file",
                    url=f"tg-file://{file_id}",
                    mime_type=get_str(document, "mime_type"),
                )
            )

    sticker = get_mapping(message, "sticker")
    if sticker is not None:
        file_id = get_str(sticker, "file_id")
        if file_id:
            segments.append(
                StickerSegment(id=f"tg-file://{file_id}", name=get_str(sticker, "emoji"))
            )

    voice = get_mapping(message, "voice")
    if voice is not None:
        file_id = get_str(voice, "file_id")
        if file_id:
            duration = get_int(voice, "duration")
            segments.append(
                VoiceSegment(
                    url=f"tg-file://{file_id}",
                    mime_type=get_str(voice, "mime_type"),
                    duration_ms=duration * 1_000 if duration is not None else None,
                )
            )

    if not segments:
        placeholder = _first_unsupported_key(message)
        if placeholder is None:
            return None
        segments.append(TextSegment(text=f"[暂不支持的消息类型: {placeholder}]"))

    mentions_self = _mentions_bot(get_str(message, "text") or "", message, bot_username, self_id)
    if mentions_self:
        segments.insert(0, AtSegment(target_id=str(self_id), display_name=bot_username))

    reply_to: str | None = None
    replies_to_self = False
    replied = get_mapping(message, "reply_to_message")
    if replied is not None:
        reply_to = get_id(replied, "message_id")
        replied_sender = get_mapping(replied, "from")
        if replied_sender is not None and get_id(replied_sender, "id") == str(self_id):
            replies_to_self = True

    envelope = MessageEnvelope(
        id=telegram_envelope_id(connection_id, chat_id, message_id),
        connection_id=connection_id,
        platform=Platform.TELEGRAM,
        chat_kind=chat_kind,
        chat_id=chat_id,
        sender_identity_id=telegram_identity(sender_id),
        occurred_at=datetime.fromtimestamp(occurred, tz=UTC),
        segments=tuple(segments),
        reply_to_message_id=reply_to,
        raw_ref=cast(FrozenJsonValue, as_json_value(update)),
    )
    return InboundEvent(
        envelope=envelope,
        mentions_self=mentions_self,
        replies_to_self=replies_to_self,
        sender_is_bot=sender.get("is_bot") is True,
    )


def encode_telegram_reply(
    plan: ReplyPlan,
    *,
    capabilities: PlatformCapabilities,
    reply_to_message_id: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Purely encode a reply plan into Telegram Bot API method/body pairs."""

    actions: list[dict[str, object]] = []
    for index, text in enumerate(plan.text_segments):
        body: dict[str, object] = {"text": text}
        if index == 0 and reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id
            body["allow_sending_without_reply"] = True
        actions.append({"method": "sendMessage", "body": body})
    for media in plan.media_segments:
        if isinstance(media, ImageSegment) and capabilities.images:
            actions.append({"method": "sendPhoto", "body": {"photo": _telegram_locator(media.url)}})
        elif isinstance(media, StickerSegment) and capabilities.stickers:
            actions.append(
                {"method": "sendSticker", "body": {"sticker": _telegram_locator(media.id)}}
            )
        elif isinstance(media, VoiceSegment) and capabilities.voice_messages:
            actions.append({"method": "sendVoice", "body": {"voice": _telegram_locator(media.url)}})
        else:
            fallback = (
                f"[图片: {media.alt_text or media.url}]"
                if isinstance(media, ImageSegment)
                else f"[表情: {media.name or media.id}]"
                if isinstance(media, StickerSegment)
                else "[语音消息]"
            )
            actions.append({"method": "sendMessage", "body": {"text": fallback}})
    return tuple(actions)


def _telegram_locator(value: str) -> str:
    return value.removeprefix("tg-file://")


def _photo_area(size: RawMapping) -> int:
    width = get_int(size, "width") or 0
    height = get_int(size, "height") or 0
    return width * height


def _first_unsupported_key(message: Mapping[str, object]) -> str | None:
    for key in _UNSUPPORTED_CONTENT_KEYS:
        if key in message:
            return key
    return None


def _mentions_bot(
    text: str,
    message: RawMapping,
    bot_username: str,
    self_id: int,
) -> bool:
    expected = f"@{bot_username}".casefold()
    for raw_entity in get_sequence(message, "entities"):
        entity = as_string_mapping(raw_entity)
        if entity is None:
            continue
        entity_type = get_str(entity, "type")
        offset = get_int(entity, "offset")
        length = get_int(entity, "length")
        if entity_type == "mention" and offset is not None and length is not None:
            if text[offset : offset + length].casefold() == expected:
                return True
        elif entity_type == "text_mention":
            user = get_mapping(entity, "user")
            if user is not None and get_id(user, "id") == str(self_id):
                return True
    return False
