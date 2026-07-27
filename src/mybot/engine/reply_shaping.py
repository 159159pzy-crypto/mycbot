"""Shape raw model text into a validated 1-3 segment reply plan."""

import json
import re
from collections.abc import Sequence
from typing import cast

from pydantic import ValidationError

from mybot.contracts import (
    Citation,
    ImageSegment,
    PlatformCapabilities,
    ReplyPlan,
    StickerSegment,
    TypingProfile,
)

_SEGMENT_CHAR_CAP = 3_500
_MAX_RENDERED_SOURCES = 5
EMPTY_REPLY_FALLBACK = "抱歉, 这次没能组织好回复, 请稍后再试。"
_MEDIA_LINE = re.compile(r"^\[\[media:(\{.*\})\]\]$")
_MEME_LINE = re.compile(r"^\[\[meme:([a-z0-9_.-]{1,64})\]\]$")
_MAX_MEDIA_SEGMENTS = 4


def shape_reply(
    text: str,
    *,
    capabilities: PlatformCapabilities,
    citations: Sequence[Citation] = (),
    empty_fallback: str = EMPTY_REPLY_FALLBACK,
) -> ReplyPlan:
    cleaned, media_segments, meme_intent = _extract_directives(text)
    paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [empty_fallback]
    if len(paragraphs) > 3:
        paragraphs = [paragraphs[0], paragraphs[1], "\n\n".join(paragraphs[2:])]

    rendered = list(citations[:_MAX_RENDERED_SOURCES])
    if rendered:
        footer = _sources_footer(rendered)
        last = _truncate(paragraphs[-1], reserve=len(footer) + 2)
        paragraphs[-1] = f"{last}\n\n{footer}"

    segments = tuple(_truncate(part) for part in paragraphs)
    return ReplyPlan(
        text_segments=segments,
        media_segments=media_segments,
        citations=tuple(citations),
        meme_intent=meme_intent,
        typing=TypingProfile(enabled=capabilities.typing),
    )


def _extract_directives(
    text: str,
) -> tuple[str, tuple[ImageSegment | StickerSegment, ...], str | None]:
    visible: list[str] = []
    media: list[ImageSegment | StickerSegment] = []
    meme_intent: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        media_match = _MEDIA_LINE.fullmatch(stripped)
        if media_match is not None and len(media) < _MAX_MEDIA_SEGMENTS:
            parsed = _parse_media(media_match.group(1))
            if parsed is not None:
                media.append(parsed)
                continue
        meme_match = _MEME_LINE.fullmatch(stripped)
        if meme_match is not None:
            meme_intent = meme_match.group(1)
            continue
        visible.append(line)
    return "\n".join(visible).strip(), tuple(media), meme_intent


def _parse_media(raw: str) -> ImageSegment | StickerSegment | None:
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    payload = cast(dict[str, object], decoded)
    kind = payload.get("type")
    try:
        if kind == "image":
            segment = ImageSegment.model_validate(payload)
            if not segment.url.startswith(("https://", "http://", "tg-file://")):
                return None
            return segment
        if kind == "sticker":
            return StickerSegment.model_validate(payload)
    except ValidationError:
        return None
    return None


def _sources_footer(citations: Sequence[Citation]) -> str:
    lines = ["参考来源:"]
    for index, citation in enumerate(citations, start=1):
        if citation.label == citation.uri:
            lines.append(f"[{index}] {citation.uri}")
        else:
            lines.append(f"[{index}] {citation.label} — {citation.uri}")
    return "\n".join(lines)


def _truncate(text: str, *, reserve: int = 0) -> str:
    cap = max(200, _SEGMENT_CHAR_CAP - reserve)
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"
