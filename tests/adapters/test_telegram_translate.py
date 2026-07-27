from datetime import UTC, datetime

import pytest
from platform_payloads import (
    telegram_group_message,
    telegram_private_message,
    telegram_update,
    telegram_user,
)

from mybot.adapters import InboundEvent
from mybot.adapters.telegram.translate import (
    TELEGRAM_CAPABILITIES,
    encode_telegram_reply,
    translate_telegram_update,
)
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

CONNECTION = "telegram-main"
BOT_ID = 424242
BOT_USERNAME = "mybot_bot"


def translate(update: dict[str, object]) -> InboundEvent:
    event = translate_telegram_update(
        update,
        connection_id=CONNECTION,
        self_id=BOT_ID,
        bot_username=BOT_USERNAME,
    )
    assert event is not None
    return event


def test_private_text_message_maps_to_direct_envelope() -> None:
    event = translate(telegram_update(telegram_private_message()))
    envelope = event.envelope

    assert envelope.platform is Platform.TELEGRAM
    assert envelope.chat_kind is ChatKind.DIRECT
    assert envelope.connection_id == CONNECTION
    assert envelope.chat_id == "777"
    assert envelope.sender_identity_id == "telegram:777"
    assert envelope.id == "telegram:telegram-main:777:55"
    assert envelope.occurred_at == datetime.fromtimestamp(1_784_000_000, tz=UTC)
    assert envelope.segments == (TextSegment(text="hello bot"),)
    assert event.mentions_self is False
    assert event.replies_to_self is False


def test_bot_sender_flag_maps_from_is_bot() -> None:
    human = translate(telegram_update(telegram_private_message()))
    robot = translate(
        telegram_update(
            telegram_private_message(**{"from": telegram_user(user_id=999, is_bot=True)})
        )
    )

    assert human.sender_is_bot is False
    assert robot.sender_is_bot is True


def test_supergroup_maps_to_group_chat_kind() -> None:
    envelope = translate(telegram_update(telegram_group_message())).envelope

    assert envelope.chat_kind is ChatKind.GROUP
    assert envelope.chat_id == "-1001234"


def test_mention_entity_matching_bot_username_sets_mentions_self() -> None:
    message = telegram_group_message(
        text=f"hey @{BOT_USERNAME} status?",
        entities=[{"type": "mention", "offset": 4, "length": len(BOT_USERNAME) + 1}],
    )
    event = translate(telegram_update(message))

    assert event.mentions_self is True
    assert AtSegment(target_id=str(BOT_ID), display_name=BOT_USERNAME) in event.envelope.segments


def test_mention_of_other_user_does_not_set_mentions_self() -> None:
    message = telegram_group_message(
        text="hey @someone_else status?",
        entities=[{"type": "mention", "offset": 4, "length": 13}],
    )
    event = translate(telegram_update(message))

    assert event.mentions_self is False


def test_reply_to_bot_message_sets_replies_to_self_and_reference() -> None:
    message = telegram_group_message(
        reply_to_message={
            "message_id": 41,
            "from": telegram_user(user_id=BOT_ID, is_bot=True),
            "chat": {"id": -100_1234, "type": "supergroup", "title": "ops"},
            "date": 1_783_999_000,
            "text": "earlier bot reply",
        }
    )
    event = translate(telegram_update(message))

    assert event.replies_to_self is True
    assert event.envelope.reply_to_message_id == "41"


def test_reply_to_human_message_is_not_replies_to_self() -> None:
    message = telegram_group_message(
        reply_to_message={
            "message_id": 42,
            "from": telegram_user(user_id=999),
            "chat": {"id": -100_1234, "type": "supergroup", "title": "ops"},
            "date": 1_783_999_000,
            "text": "human words",
        }
    )
    event = translate(telegram_update(message))

    assert event.replies_to_self is False
    assert event.envelope.reply_to_message_id == "42"


def test_photo_uses_largest_size_and_caption() -> None:
    message = telegram_private_message(
        text=None,
        photo=[
            {"file_id": "small", "width": 90, "height": 90},
            {"file_id": "big", "width": 800, "height": 800},
        ],
        caption="sunset",
    )
    segments = translate(telegram_update(message)).envelope.segments

    assert TextSegment(text="sunset") in segments
    assert ImageSegment(url="tg-file://big") in segments


def test_document_maps_to_file_segment() -> None:
    message = telegram_private_message(
        text=None,
        document={
            "file_id": "doc-1",
            "file_name": "notes.pdf",
            "mime_type": "application/pdf",
        },
    )
    segments = translate(telegram_update(message)).envelope.segments

    assert segments == (
        FileSegment(name="notes.pdf", url="tg-file://doc-1", mime_type="application/pdf"),
    )


def test_image_document_maps_to_image_segment_for_vision() -> None:
    message = telegram_private_message(
        text=None,
        document={
            "file_id": "image-doc-1",
            "file_name": "original.png",
            "mime_type": "image/png",
        },
    )

    segments = translate(telegram_update(message)).envelope.segments

    assert segments == (
        ImageSegment(url="tg-file://image-doc-1", alt_text="original.png"),
    )


def test_service_message_returns_none() -> None:
    message = telegram_private_message(text=None, new_chat_members=[telegram_user(user_id=1)])
    update = telegram_update(message)

    assert (
        translate_telegram_update(
            update, connection_id=CONNECTION, self_id=BOT_ID, bot_username=BOT_USERNAME
        )
        is None
    )


def test_update_without_message_returns_none() -> None:
    assert (
        translate_telegram_update(
            {"update_id": 9}, connection_id=CONNECTION, self_id=BOT_ID, bot_username=BOT_USERNAME
        )
        is None
    )


def test_sticker_and_voice_map_to_typed_segments() -> None:
    message = telegram_private_message(
        text=None,
        sticker={"file_id": "st-1", "emoji": "😀"},
        voice={"file_id": "voice-1", "duration": 2, "mime_type": "audio/ogg"},
    )
    segments = translate(telegram_update(message)).envelope.segments

    assert segments == (
        StickerSegment(id="tg-file://st-1", name="😀"),
        VoiceSegment(url="tg-file://voice-1", mime_type="audio/ogg", duration_ms=2_000),
    )


def test_raw_ref_is_frozen_against_mutation() -> None:
    envelope = translate(telegram_update(telegram_private_message())).envelope

    assert envelope.raw_ref is not None
    with pytest.raises(TypeError):
        envelope.raw_ref["update_id"] = 2  # type: ignore[index]


def test_telegram_capabilities() -> None:
    assert TELEGRAM_CAPABILITIES.replies is True
    assert TELEGRAM_CAPABILITIES.typing is True
    assert TELEGRAM_CAPABILITIES.editing is True
    assert TELEGRAM_CAPABILITIES.mentions is True
    assert TELEGRAM_CAPABILITIES.images is True
    assert TELEGRAM_CAPABILITIES.stickers is True
    assert TELEGRAM_CAPABILITIES.voice_messages is True


def test_telegram_outbound_encoder_selects_bot_api_methods() -> None:
    plan = ReplyPlan(
        text_segments=("hello",),
        media_segments=(
            ImageSegment(url="https://img.example/cat.png"),
            StickerSegment(id="tg-file://sticker-1"),
            VoiceSegment(url="tg-file://voice-1"),
        ),
    )

    actions = encode_telegram_reply(
        plan,
        capabilities=TELEGRAM_CAPABILITIES,
        reply_to_message_id="77",
    )

    assert [action["method"] for action in actions] == [
        "sendMessage",
        "sendPhoto",
        "sendSticker",
        "sendVoice",
    ]
    assert actions[0]["body"]["reply_to_message_id"] == "77"
