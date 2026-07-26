"""Shape raw model text into a validated 1-3 segment reply plan."""

from collections.abc import Sequence

from mybot.contracts import Citation, PlatformCapabilities, ReplyPlan, TypingProfile

_SEGMENT_CHAR_CAP = 3_500
_MAX_RENDERED_SOURCES = 5
EMPTY_REPLY_FALLBACK = "Sorry — I could not compose a reply this time. Please try again."


def shape_reply(
    text: str,
    *,
    capabilities: PlatformCapabilities,
    citations: Sequence[Citation] = (),
) -> ReplyPlan:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [EMPTY_REPLY_FALLBACK]
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
        citations=tuple(citations),
        typing=TypingProfile(enabled=capabilities.typing),
    )


def _sources_footer(citations: Sequence[Citation]) -> str:
    lines = ["Sources:"]
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
