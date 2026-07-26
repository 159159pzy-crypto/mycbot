from mybot.contracts import ChatKind, Platform
from mybot.engine.prompt import HistoryEntry, assemble_messages, estimate_tokens


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
