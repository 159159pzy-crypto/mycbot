"""Prompt assembly with mixed Chinese/Latin token estimates and media references."""

import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from mybot.contracts import (
    AtSegment,
    ChatKind,
    FileSegment,
    ImageSegment,
    MessageEnvelope,
    Platform,
    ReferenceSegment,
    StickerSegment,
    TextSegment,
    VoiceSegment,
)
from mybot.infrastructure.llm import (
    ChatContent,
    ChatMessage,
    ImageContentPart,
    ImageUrl,
    TextContentPart,
)

_REPLY_RULES = (
    "Reply in plain text without markdown headings or bullet lists. "
    "Keep replies short: at most three short paragraphs."
)


@dataclass(slots=True, frozen=True)
class HistoryEntry:
    """One prior conversation message, newest-first when passed in sequences."""

    direction: str  # "inbound" | "outbound"
    sender: str
    text: str
    source_id: str | None = None


@dataclass(slots=True, frozen=True)
class HistoryPartition:
    kept: tuple[HistoryEntry, ...]
    dropped: tuple[HistoryEntry, ...]


@dataclass(slots=True, frozen=True)
class EnvelopePrompt:
    summary: str
    content: ChatContent


def estimate_tokens(text: str) -> int:
    cjk = 0
    latin = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        elif not char.isspace():
            latin += 1
    return max(1, math.ceil(cjk / 1.5) + math.ceil(latin / 4))


def envelope_text(envelope: MessageEnvelope) -> str:
    parts = [
        segment.text for segment in envelope.segments if isinstance(segment, TextSegment)
    ]
    return "\n".join(parts).strip()


def envelope_prompt(envelope: MessageEnvelope, *, include_images: bool) -> EnvelopePrompt:
    """Render readable media references and optionally preserve OpenAI image parts."""

    summary_parts: list[str] = []
    content_parts: list[TextContentPart | ImageContentPart] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if not pending_text:
            return
        text = "\n".join(pending_text).strip()
        pending_text.clear()
        if text:
            content_parts.append(TextContentPart(text=text))

    for segment in envelope.segments:
        if isinstance(segment, TextSegment):
            summary_parts.append(segment.text)
            pending_text.append(segment.text)
        elif isinstance(segment, ImageSegment):
            label = segment.alt_text or "图片"
            summary_parts.append(f"[图片: {label}]")
            if include_images:
                flush_text()
                content_parts.append(
                    ImageContentPart(image_url=ImageUrl(url=segment.url))
                )
            else:
                reference = (
                    "Telegram 图片" if segment.url.startswith("tg-file://") else segment.url
                )
                pending_text.append(f"[图片: {label}; 引用: {reference}]")
        else:
            readable = _segment_reference(segment)
            if readable:
                summary_parts.append(readable)
                pending_text.append(readable)
    flush_text()
    summary = "\n".join(summary_parts).strip() or "[非文本消息]"
    if not content_parts:
        content: ChatContent = summary
    elif len(content_parts) == 1 and isinstance(content_parts[0], TextContentPart):
        content = content_parts[0].text
    else:
        content = tuple(content_parts)
    return EnvelopePrompt(summary=summary, content=content)


def assemble_messages(
    *,
    system_prompt: str,
    platform: Platform,
    chat_kind: ChatKind,
    history: Sequence[HistoryEntry],
    inbound_sender: str,
    inbound_text: str,
    token_budget: int,
    inbound_content: ChatContent | None = None,
) -> list[ChatMessage]:
    """Build system + windowed history + inbound; oldest history drops first."""

    system_content = _system_content(system_prompt, platform, chat_kind)
    grouped = chat_kind is not ChatKind.DIRECT
    raw_inbound: ChatContent = inbound_content if inbound_content is not None else inbound_text
    rendered_inbound = _prefix_content(raw_inbound, inbound_sender) if grouped else raw_inbound

    partition = partition_history(
        system_prompt=system_prompt,
        platform=platform,
        chat_kind=chat_kind,
        history=history,
        inbound_text=inbound_text,
        token_budget=token_budget,
    )
    window: list[ChatMessage] = []
    for entry in partition.kept:
        if entry.direction == "outbound":
            content = entry.text
            role = "assistant"
        else:
            content = f"{entry.sender}: {entry.text}" if grouped else entry.text
            role = "user"
        window.append(ChatMessage(role=role, content=content))
    window.reverse()

    return [
        ChatMessage(role="system", content=system_content),
        *window,
        ChatMessage(role="user", content=rendered_inbound),
    ]


def partition_history(
    *,
    system_prompt: str,
    platform: Platform,
    chat_kind: ChatKind,
    history: Sequence[HistoryEntry],
    inbound_text: str,
    token_budget: int,
    max_messages: int | None = None,
) -> HistoryPartition:
    """Split newest-first history into prompt-visible and pre-truncation rows."""

    system_content = _system_content(system_prompt, platform, chat_kind)
    remaining = token_budget - estimate_tokens(system_content) - estimate_tokens(inbound_text)
    grouped = chat_kind is not ChatKind.DIRECT
    kept: list[HistoryEntry] = []
    for index, entry in enumerate(history):
        if max_messages is not None and len(kept) >= max_messages:
            return HistoryPartition(tuple(kept), tuple(history[index:]))
        content = (
            entry.text
            if entry.direction == "outbound" or not grouped
            else f"{entry.sender}: {entry.text}"
        )
        cost = estimate_tokens(content)
        if cost > remaining:
            return HistoryPartition(tuple(kept), tuple(history[index:]))
        remaining -= cost
        kept.append(entry)
    return HistoryPartition(tuple(kept), ())


def _system_content(system_prompt: str, platform: Platform, chat_kind: ChatKind) -> str:
    return (
        f"{system_prompt}\n\n"
        f"You are chatting on {platform.value} in a {chat_kind.value.lower()} chat. "
        f"{_REPLY_RULES}"
    )


def _prefix_content(content: ChatContent, sender: str) -> ChatContent:
    if isinstance(content, str):
        return f"{sender}: {content}"
    parts = list(content)
    if parts and isinstance(parts[0], TextContentPart):
        parts[0] = TextContentPart(text=f"{sender}: {parts[0].text}")
    else:
        parts.insert(0, TextContentPart(text=f"{sender}:"))
    return tuple(parts)


def _segment_reference(
    segment: FileSegment | ReferenceSegment | AtSegment | StickerSegment | VoiceSegment,
) -> str:
    if isinstance(segment, FileSegment):
        return f"[文件: {segment.name}]"
    if isinstance(segment, ReferenceSegment):
        return f"[引用消息: {segment.label or segment.message_id}]"
    if isinstance(segment, AtSegment):
        return f"[@{segment.display_name or segment.target_id}]"
    if isinstance(segment, StickerSegment):
        return f"[表情: {segment.name or segment.id}]"
    return "[语音消息]"


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or unicodedata.name(char, "").startswith(("HIRAGANA", "KATAKANA", "HANGUL"))
    )
