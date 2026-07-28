from datetime import UTC, datetime

import pytest
from platform_payloads import onebot_group_message, onebot_private_message

from mybot.adapters import InboundEvent
from mybot.adapters.qq.translate import QQ_CAPABILITIES, encode_qq_reply, translate_qq_event
from mybot.contracts import (
    AtSegment,
    ChatKind,
    FileSegment,
    ImageSegment,
    Platform,
    ReplyPlan,
    StickerSegment,
    TextSegment,
    VoiceSegment,
)

CONNECTION = "qq-main"
SELF_ID = 10000


def translate(payload: dict[str, object]) -> InboundEvent:
    event = translate_qq_event(payload, connection_id=CONNECTION, self_id=SELF_ID)
    assert event is not None
    return event


def test_private_text_message_maps_to_direct_envelope() -> None:
    event = translate(onebot_private_message())
    envelope = event.envelope

    assert envelope.platform is Platform.QQ
    assert envelope.chat_kind is ChatKind.DIRECT
    assert envelope.connection_id == CONNECTION
    assert envelope.chat_id == "10001"
    assert envelope.sender_identity_id == "qq:10001"
    assert envelope.id == "qq:qq-main:901"
    assert envelope.occurred_at == datetime.fromtimestamp(1_784_000_000, tz=UTC)
    assert envelope.occurred_at.tzinfo is not None
    assert envelope.segments == (TextSegment(text="hello bot"),)
    assert event.mentions_self is False
    assert event.replies_to_self is False


def test_group_message_maps_group_chat_and_deterministic_id() -> None:
    envelope = translate(onebot_group_message(message_id=77, group_id=333)).envelope

    assert envelope.chat_kind is ChatKind.GROUP
    assert envelope.chat_id == "333"
    assert envelope.id == "qq:qq-main:77"


def test_at_self_segment_sets_mention_and_preserves_typed_segment() -> None:
    payload = onebot_group_message(
        message=[
            {"type": "at", "data": {"qq": str(SELF_ID)}},
            {"type": "text", "data": {"text": " ping"}},
        ]
    )
    event = translate(payload)

    assert event.mentions_self is True
    assert event.envelope.segments == (
        AtSegment(target_id=str(SELF_ID)),
        TextSegment(text="ping"),
    )


def test_at_other_users_do_not_mention_self() -> None:
    payload = onebot_group_message(
        message=[
            {"type": "at", "data": {"qq": "555"}},
            {"type": "text", "data": {"text": "hi"}},
        ]
    )
    event = translate(payload)

    assert event.mentions_self is False
    assert event.envelope.segments[0] == AtSegment(target_id="555")


def test_at_only_mention_is_a_valid_typed_message() -> None:
    payload = onebot_group_message(message=[{"type": "at", "data": {"qq": str(SELF_ID)}}])
    event = translate(payload)

    assert event.mentions_self is True
    assert event.envelope.segments == (AtSegment(target_id=str(SELF_ID)),)


def test_image_and_file_segments_map_with_locators() -> None:
    payload = onebot_private_message(
        message=[
            {"type": "text", "data": {"text": "look"}},
            {"type": "image", "data": {"file": "abc.image", "url": "https://img.example/x"}},
            {"type": "file", "data": {"name": "notes.txt", "file": "file-id-1"}},
        ]
    )
    segments = translate(payload).envelope.segments

    assert segments[0] == TextSegment(text="look")
    assert segments[1] == ImageSegment(url="https://img.example/x")
    file_segment = segments[2]
    assert isinstance(file_segment, FileSegment)
    assert file_segment.name == "notes.txt"
    assert file_segment.url == "file-id-1"


def test_reply_segment_sets_reply_to_message_id() -> None:
    payload = onebot_group_message(
        message=[
            {"type": "reply", "data": {"id": "888"}},
            {"type": "text", "data": {"text": "agreed"}},
        ]
    )
    envelope = translate(payload).envelope

    assert envelope.reply_to_message_id == "888"
    assert envelope.segments == (TextSegment(text="agreed"),)


def test_face_and_record_map_to_sticker_and_voice() -> None:
    payload = onebot_private_message(
        message=[
            {"type": "face", "data": {"id": "14"}},
            {"type": "record", "data": {"file": "voice.amr", "url": "https://qq/voice"}},
        ]
    )
    segments = translate(payload).envelope.segments

    assert segments == (
        StickerSegment(id="14"),
        VoiceSegment(url="https://qq/voice"),
    )


def test_unsupported_segment_still_degrades_to_placeholder() -> None:
    payload = onebot_private_message(message=[{"type": "json", "data": {"data": "{}"}}])

    segment = translate(payload).envelope.segments[0]

    assert isinstance(segment, TextSegment)
    assert "json" in segment.text


def test_non_message_events_return_none() -> None:
    heartbeat = {"post_type": "meta_event", "meta_event_type": "heartbeat", "time": 1}
    notice = {"post_type": "notice", "notice_type": "group_increase", "time": 1}

    assert translate_qq_event(heartbeat, connection_id=CONNECTION, self_id=SELF_ID) is None
    assert translate_qq_event(notice, connection_id=CONNECTION, self_id=SELF_ID) is None


def test_contentless_message_returns_none() -> None:
    payload = onebot_private_message(message=[])
    assert translate_qq_event(payload, connection_id=CONNECTION, self_id=SELF_ID) is None


def test_raw_ref_is_frozen_against_mutation() -> None:
    envelope = translate(onebot_private_message()).envelope

    assert envelope.raw_ref is not None
    with pytest.raises(TypeError):
        envelope.raw_ref["message_type"] = "group"  # type: ignore[index]


def test_qq_capabilities_reflect_onebot_v11() -> None:
    assert QQ_CAPABILITIES.replies is True
    assert QQ_CAPABILITIES.typing is False
    assert QQ_CAPABILITIES.editing is False
    assert QQ_CAPABILITIES.mentions is True
    assert QQ_CAPABILITIES.images is True
    assert QQ_CAPABILITIES.stickers is True
    assert QQ_CAPABILITIES.voice_messages is True


def test_qq_meme_intent_maps_to_face_and_unmapped_intent_is_visible() -> None:
    mapped = encode_qq_reply(
        ReplyPlan(text_segments=("ok",), meme_intent="acknowledge"),
        capabilities=QQ_CAPABILITIES,
        meme_intents={"acknowledge": "14"},
    )
    fallback = encode_qq_reply(
        ReplyPlan(text_segments=("ok",), meme_intent="unknown"),
        capabilities=QQ_CAPABILITIES,
        meme_intents={"acknowledge": "14"},
    )

    assert mapped[0][-1] == {"type": "face", "data": {"id": "14"}}
    assert fallback[0][-1]["data"]["text"] == "[表情意图: unknown]"


def test_qq_outbound_encoder_renders_media_and_readable_fallbacks() -> None:
    plan = ReplyPlan(
        text_segments=("hello",),
        media_segments=(
            ImageSegment(url="https://img.example/cat.png", alt_text="cat"),
            StickerSegment(id="14"),
            VoiceSegment(url="voice.amr"),
        ),
    )

    encoded = encode_qq_reply(
        plan,
        capabilities=QQ_CAPABILITIES,
        reply_to_message_id="88",
    )

    assert encoded[0][0] == {"type": "reply", "data": {"id": "88"}}
    assert {segment["type"] for segment in encoded[0]} == {
        "reply",
        "text",
        "image",
        "face",
        "record",
    }

    text_only = encode_qq_reply(plan, capabilities=QQ_CAPABILITIES.model_copy(
        update={"images": False, "stickers": False, "voice_messages": False}
    ))
    rendered = " ".join(
        str(segment["data"].get("text", ""))
        for message in text_only
        for segment in message
    )
    assert "[图片: cat]" in rendered
    assert "[表情: 14]" in rendered
    assert "[语音消息]" in rendered
