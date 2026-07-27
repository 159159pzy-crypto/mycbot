"""Validated user-visible reply plans."""

from typing import Annotated

from pydantic import Field

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.messages import ImageSegment, StickerSegment, VoiceSegment

ReplyMediaSegment = Annotated[
    ImageSegment | StickerSegment | VoiceSegment,
    Field(discriminator="type"),
]


class Citation(FrozenModel):
    label: NonEmptyStr
    uri: NonEmptyStr


class TypingProfile(FrozenModel):
    enabled: bool = False
    initial_delay_ms: int = Field(default=0, ge=0, le=30_000)
    chars_per_second: float = Field(default=18.0, gt=0.0, le=100.0)


class ReplyPlan(FrozenModel):
    text_segments: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1, max_length=3)]
    media_segments: tuple[ReplyMediaSegment, ...] = ()
    citations: tuple[Citation, ...] = ()
    meme_intent: NonEmptyStr | None = None
    typing: TypingProfile = Field(default_factory=TypingProfile)
