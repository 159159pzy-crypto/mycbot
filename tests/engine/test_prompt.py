from datetime import UTC, datetime

from mybot.contracts import ChatKind, ImageSegment, MessageEnvelope, Platform, TextSegment
from mybot.engine.prompt import (
    HistoryEntry,
    assemble_messages,
    envelope_prompt,
    estimate_tokens,
    partition_history,
)
from mybot.infrastructure.llm import ImageContentPart, ImageUrl, TextContentPart


def history(count: int) -> list[HistoryEntry]:
    entries: list[HistoryEntry] = []
    for index in range(count):
        direction = "outbound" if index % 2 == 0 else "inbound"
        entries.append(
            HistoryEntry(
                direction=direction,
                sender="self" if direction == "outbound" else "qq:10001",
                text=f"message number {count - index}",
            )
        )
    return entries  # newest first


def test_estimate_tokens_is_positive_and_monotonic() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("word") >= 1
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 100)


def test_estimate_tokens_calibrates_mixed_cjk_and_latin_text() -> None:
    assert estimate_tokens("中文" * 20) > estimate_tokens("ab" * 20)
    mixed = estimate_tokens("请检查 deploy status 和 error logs")
    assert estimate_tokens("请检查") < mixed < estimate_tokens("请检查" * 10)


def test_partition_history_exposes_rows_before_count_truncation() -> None:
    entries = tuple(
        HistoryEntry("inbound", "u", f"message-{index}", source_id=str(index))
        for index in range(5)
    )

    partition = partition_history(
        system_prompt="system",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        history=entries,
        inbound_text="current",
        token_budget=1_000,
        max_messages=2,
    )

    assert [entry.source_id for entry in partition.kept] == ["0", "1"]
    assert [entry.source_id for entry in partition.dropped] == ["2", "3", "4"]


def test_envelope_prompt_preserves_text_and_image_references() -> None:
    envelope = MessageEnvelope(
        id="qq:main:1",
        connection_id="main",
        platform=Platform.QQ,
        chat_kind=ChatKind.DIRECT,
        chat_id="42",
        sender_identity_id="qq:7",
        occurred_at=datetime.now(tz=UTC),
        segments=(
            TextSegment(text="这是什么?"),
            ImageSegment(url="https://img.example/cat.png", alt_text="随手拍"),
        ),
    )

    prepared = envelope_prompt(envelope, include_images=True)

    assert prepared.summary == "这是什么?\n[图片: 随手拍]"
    assert prepared.content == (
        TextContentPart(text="这是什么?"),
        ImageContentPart(
            image_url=ImageUrl(url="https://img.example/cat.png")
        ),
    )


def test_system_message_carries_persona_context_and_reply_rules() -> None:
    messages = assemble_messages(
        system_prompt="You are MyBot.",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.GROUP,
        history=[],
        inbound_sender="telegram:777",
        inbound_text="hello",
        token_budget=1_000,
    )

    system = messages[0]
    assert system.role == "system"
    assert "You are MyBot." in system.content
    assert "TELEGRAM" in system.content
    assert "group chat" in system.content
    assert "plain text" in system.content
    assert "three short paragraphs" in system.content


def test_history_is_oldest_first_with_roles_and_group_attribution() -> None:
    entries = [
        HistoryEntry(direction="inbound", sender="qq:2", text="second question"),
        HistoryEntry(direction="outbound", sender="self", text="first answer"),
        HistoryEntry(direction="inbound", sender="qq:1", text="first question"),
    ]

    messages = assemble_messages(
        system_prompt="persona",
        platform=Platform.QQ,
        chat_kind=ChatKind.GROUP,
        history=entries,
        inbound_sender="qq:2",
        inbound_text="and now?",
        token_budget=10_000,
    )

    middle = messages[1:-1]
    assert [message.role for message in middle] == ["user", "assistant", "user"]
    assert middle[0].content == "qq:1: first question"
    assert middle[1].content == "first answer"
    assert middle[2].content == "qq:2: second question"
    assert messages[-1].content == "qq:2: and now?"


def test_direct_chats_do_not_prefix_senders() -> None:
    messages = assemble_messages(
        system_prompt="persona",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        history=[HistoryEntry(direction="inbound", sender="telegram:777", text="earlier")],
        inbound_sender="telegram:777",
        inbound_text="now",
        token_budget=10_000,
    )

    assert messages[1].content == "earlier"
    assert messages[-1].content == "now"


def test_small_budget_drops_oldest_history_but_never_the_inbound_message() -> None:
    entries = history(30)

    generous = assemble_messages(
        system_prompt="persona",
        platform=Platform.QQ,
        chat_kind=ChatKind.DIRECT,
        history=entries,
        inbound_sender="qq:10001",
        inbound_text="latest",
        token_budget=100_000,
    )
    tight = assemble_messages(
        system_prompt="persona",
        platform=Platform.QQ,
        chat_kind=ChatKind.DIRECT,
        history=entries,
        inbound_sender="qq:10001",
        inbound_text="latest",
        token_budget=80,
    )

    assert len(generous) == 32  # system + 30 history + inbound
    assert 2 <= len(tight) < len(generous)
    assert tight[-1].content == "latest"
    kept_history = [message.content for message in tight[1:-1]]
    # the newest history survives; the oldest is dropped first
    assert "message number 30" in kept_history[-1]
