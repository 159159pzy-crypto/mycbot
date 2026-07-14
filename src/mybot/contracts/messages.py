"""Platform-neutral inbound message and capability contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr, require_utc
from mybot.contracts.json import FrozenJsonValueField


class Platform(StrEnum):
    QQ = "QQ"
    TELEGRAM = "TELEGRAM"


class ChatKind(StrEnum):
    DIRECT = "DIRECT"
    GROUP = "GROUP"
    CHANNEL = "CHANNEL"


class TextSegment(FrozenModel):
    type: Literal["text"] = "text"
    text: NonEmptyStr


class ImageSegment(FrozenModel):
    type: Literal["image"] = "image"
    url: NonEmptyStr
    alt_text: NonEmptyStr | None = None


class FileSegment(FrozenModel):
    type: Literal["file"] = "file"
    name: NonEmptyStr
    url: NonEmptyStr
    mime_type: NonEmptyStr | None = None


class ReferenceSegment(FrozenModel):
    type: Literal["reference"] = "reference"
    message_id: NonEmptyStr
    label: NonEmptyStr | None = None


MessageSegment = Annotated[
    TextSegment | ImageSegment | FileSegment | ReferenceSegment,
    Field(discriminator="type"),
]


class MessageEnvelope(FrozenModel):
    id: NonEmptyStr
    connection_id: NonEmptyStr
    platform: Platform
    chat_kind: ChatKind
    chat_id: NonEmptyStr
    thread_id: NonEmptyStr | None = None
    sender_identity_id: NonEmptyStr
    occurred_at: datetime
    segments: Annotated[tuple[MessageSegment, ...], Field(min_length=1)]
    reply_to_message_id: NonEmptyStr | None = None
    raw_ref: FrozenJsonValueField | None = None

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        validated = require_utc(value, field_name="occurred_at")
        assert validated is not None
        return validated


class PlatformCapabilities(FrozenModel):
    editing: bool = False
    replies: bool = False
    typing: bool = False
    proactive_messages: bool = False
    combined_media_text: bool = False
    threads: bool = False
    reactions: bool = False
