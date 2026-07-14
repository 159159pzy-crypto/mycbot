from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    FileSegment,
    ImageSegment,
    MessageEnvelope,
    Platform,
    PlatformCapabilities,
    ReferenceSegment,
    TextSegment,
)


def test_message_envelope_is_immutable_and_preserves_typed_segments() -> None:
    envelope = MessageEnvelope(
        id="message-42",
        connection_id="qq-primary",
        platform=Platform.QQ,
        chat_kind=ChatKind.GROUP,
        chat_id="group/alpha",
        sender_identity_id="identity-7",
        occurred_at=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        segments=(
            TextSegment(text="hello"),
            ImageSegment(url="https://cdn.example/image.png", alt_text="diagram"),
            FileSegment(name="notes.txt", url="https://cdn.example/notes.txt"),
            ReferenceSegment(message_id="message-41", label="previous context"),
        ),
        raw_ref={"event_id": 99},
    )

    assert tuple(segment.type for segment in envelope.segments) == (
        "text",
        "image",
        "file",
        "reference",
    )
    with pytest.raises(ValidationError):
        envelope.chat_id = "different"  # type: ignore[misc]


@pytest.mark.parametrize(
    "occurred_at",
    [
        datetime(2026, 7, 14, 12, 30),
        datetime(2026, 7, 14, 20, 30, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_message_envelope_requires_an_explicit_utc_timestamp(occurred_at: datetime) -> None:
    with pytest.raises(ValidationError, match="UTC"):
        MessageEnvelope(
            id="message-42",
            connection_id="telegram-primary",
            platform=Platform.TELEGRAM,
            chat_kind=ChatKind.DIRECT,
            chat_id="chat-1",
            sender_identity_id="identity-7",
            occurred_at=occurred_at,
            segments=(TextSegment(text="hello"),),
        )


def test_message_envelope_rejects_empty_segments() -> None:
    with pytest.raises(ValidationError):
        MessageEnvelope(
            id="message-42",
            connection_id="telegram-primary",
            platform=Platform.TELEGRAM,
            chat_kind=ChatKind.DIRECT,
            chat_id="chat-1",
            sender_identity_id="identity-7",
            occurred_at=datetime.now(UTC),
            segments=(),
        )


def test_platform_capabilities_default_to_denied() -> None:
    capabilities = PlatformCapabilities()

    assert capabilities.model_dump() == {
        "editing": False,
        "replies": False,
        "typing": False,
        "proactive_messages": False,
        "combined_media_text": False,
        "threads": False,
        "reactions": False,
    }


@given(
    connection_id=st.text(min_size=1, max_size=24).filter(str.strip),
    chat_id=st.text(min_size=1, max_size=24).filter(str.strip),
    thread_id=st.one_of(st.none(), st.text(min_size=1, max_size=24).filter(str.strip)),
)
def test_conversation_key_is_deterministic_and_round_trip_safe(
    connection_id: str, chat_id: str, thread_id: str | None
) -> None:
    key = ConversationKey(
        connection_id=connection_id,
        chat_kind=ChatKind.CHANNEL,
        chat_id=chat_id,
        thread_id=thread_id,
    )

    assert key.stable_key == ConversationKey.model_validate(key.model_dump()).stable_key
    assert str(key) == key.stable_key
    assert key.stable_key.startswith("v1:")


def test_conversation_key_escapes_delimiters_to_avoid_collisions() -> None:
    first = ConversationKey(
        connection_id="a:b",
        chat_kind=ChatKind.GROUP,
        chat_id="c",
    )
    second = ConversationKey(
        connection_id="a",
        chat_kind=ChatKind.GROUP,
        chat_id="b:c",
    )

    assert first.stable_key != second.stable_key
