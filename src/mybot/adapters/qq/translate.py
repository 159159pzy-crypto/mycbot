"""Pure translation from OneBot v11 events to public message contracts."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from mybot.adapters import InboundEvent
from mybot.adapters.identity import qq_envelope_id, qq_identity
from mybot.adapters.payload import (
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

QQ_CAPABILITIES = PlatformCapabilities(
    editing=False,
    replies=True,
    typing=False,
    proactive_messages=True,
    combined_media_text=True,
    threads=False,
    reactions=False,
    images=True,
    mentions=True,
    stickers=True,
    voice_messages=True,
)

type ContentSegment = (
    TextSegment | ImageSegment | FileSegment | AtSegment | StickerSegment | VoiceSegment
)


def translate_qq_event(
    event: Mapping[str, object],
    *,
    connection_id: str,
    self_id: int,
) -> InboundEvent | None:
    """Translate one OneBot v11 event; non-message or contentless events yield None."""

    if get_str(event, "post_type") != "message":
        return None
    message_type = get_str(event, "message_type")
    message_id = get_id(event, "message_id")
    user_id = get_id(event, "user_id")
    occurred = get_int(event, "time")
    if message_id is None or user_id is None or occurred is None:
        return None

    if message_type == "private":
        chat_kind = ChatKind.DIRECT
        chat_id = user_id
    elif message_type == "group":
        chat_kind = ChatKind.GROUP
        group_id = get_id(event, "group_id")
        if group_id is None:
            return None
        chat_id = group_id
    else:
        return None

    segments: list[ContentSegment] = []
    mentions_self = False
    reply_to: str | None = None
    for raw_segment in get_sequence(event, "message"):
        segment = as_string_mapping(raw_segment)
        if segment is None:
            continue
        segment_type = get_str(segment, "type")
        data = get_mapping(segment, "data") or {}
        if segment_type == "text":
            text = (get_str(data, "text") or "").strip()
            if text:
                segments.append(TextSegment(text=text))
        elif segment_type == "image":
            url = get_str(data, "url") or get_str(data, "file")
            if url and url.strip():
                segments.append(ImageSegment(url=url.strip()))
        elif segment_type == "file":
            locator = get_str(data, "url") or get_str(data, "file")
            name = get_str(data, "name") or get_str(data, "file") or "file"
            if locator and locator.strip():
                segments.append(FileSegment(name=name, url=locator.strip()))
        elif segment_type == "reply":
            reply_to = get_id(data, "id") or reply_to
        elif segment_type == "at":
            target_id = get_id(data, "qq")
            if target_id is not None:
                segments.append(AtSegment(target_id=target_id))
            if target_id == str(self_id):
                mentions_self = True
        elif segment_type == "face":
            sticker_id = get_id(data, "id")
            if sticker_id is not None:
                segments.append(StickerSegment(id=sticker_id))
        elif segment_type == "record":
            locator = get_str(data, "url") or get_str(data, "file")
            if locator and locator.strip():
                segments.append(VoiceSegment(url=locator.strip()))
        elif segment_type is not None:
            segments.append(TextSegment(text=f"[暂不支持的消息类型: {segment_type}]"))

    if not segments:
        return None

    envelope = MessageEnvelope(
        id=qq_envelope_id(connection_id, message_id),
        connection_id=connection_id,
        platform=Platform.QQ,
        chat_kind=chat_kind,
        chat_id=chat_id,
        sender_identity_id=qq_identity(user_id),
        occurred_at=datetime.fromtimestamp(occurred, tz=UTC),
        segments=tuple(segments),
        reply_to_message_id=reply_to,
        raw_ref=cast(FrozenJsonValue, as_json_value(event)),
    )
    return InboundEvent(envelope=envelope, mentions_self=mentions_self, replies_to_self=False)


def encode_qq_reply(
    plan: ReplyPlan,
    *,
    capabilities: PlatformCapabilities,
    reply_to_message_id: str | None = None,
) -> tuple[tuple[dict[str, object], ...], ...]:
    """Purely encode a reply plan into OneBot v11 message arrays."""

    messages: list[list[dict[str, object]]] = []
    for index, text in enumerate(plan.text_segments):
        encoded: list[dict[str, object]] = []
        if index == 0 and reply_to_message_id is not None:
            encoded.append({"type": "reply", "data": {"id": reply_to_message_id}})
        encoded.append({"type": "text", "data": {"text": text}})
        messages.append(encoded)
    target = messages[-1]
    for media in plan.media_segments:
        if isinstance(media, ImageSegment) and capabilities.images:
            target.append({"type": "image", "data": {"file": media.url}})
        elif isinstance(media, StickerSegment) and capabilities.stickers:
            target.append({"type": "face", "data": {"id": media.id}})
        elif isinstance(media, VoiceSegment) and capabilities.voice_messages:
            target.append({"type": "record", "data": {"file": media.url}})
        else:
            target.append({"type": "text", "data": {"text": _media_fallback(media)}})
    return tuple(tuple(message) for message in messages)


def _media_fallback(media: ImageSegment | StickerSegment | VoiceSegment) -> str:
    if isinstance(media, ImageSegment):
        return f"[图片: {media.alt_text or media.url}]"
    if isinstance(media, StickerSegment):
        return f"[表情: {media.name or media.id}]"
    return "[语音消息]"
