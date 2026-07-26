"""Prompt assembly with a crude, documented token estimate.

Token counts use a characters/4 heuristic rather than a model tokenizer; the
budget is a safety margin, not an exact accounting, and is documented as such.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from mybot.contracts import ChatKind, MessageEnvelope, Platform, TextSegment
from mybot.infrastructure.llm import ChatMessage

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


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def envelope_text(envelope: MessageEnvelope) -> str:
    parts = [
        segment.text for segment in envelope.segments if isinstance(segment, TextSegment)
    ]
    return "\n".join(parts).strip()


def assemble_messages(
    *,
    system_prompt: str,
    platform: Platform,
    chat_kind: ChatKind,
    history: Sequence[HistoryEntry],
    inbound_sender: str,
    inbound_text: str,
    token_budget: int,
) -> list[ChatMessage]:
    """Build system + windowed history + inbound; oldest history drops first."""

    system_content = (
        f"{system_prompt}\n\n"
        f"You are chatting on {platform.value} in a {chat_kind.value.lower()} chat. "
        f"{_REPLY_RULES}"
    )
    grouped = chat_kind is not ChatKind.DIRECT
    inbound_content = f"{inbound_sender}: {inbound_text}" if grouped else inbound_text

    remaining = token_budget - estimate_tokens(system_content) - estimate_tokens(
        inbound_content
    )
    window: list[ChatMessage] = []
    for entry in history:  # newest first; stop when the budget is spent
        if entry.direction == "outbound":
            content = entry.text
            role = "assistant"
        else:
            content = f"{entry.sender}: {entry.text}" if grouped else entry.text
            role = "user"
        cost = estimate_tokens(content)
        if cost > remaining:
            break
        remaining -= cost
        window.append(ChatMessage(role=role, content=content))
    window.reverse()

    return [
        ChatMessage(role="system", content=system_content),
        *window,
        ChatMessage(role="user", content=inbound_content),
    ]
