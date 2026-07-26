from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mybot.contracts import (
    ChatKind,
    MessageEnvelope,
    Platform,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.engine.turn_policy import decide_turn, extract_command


def envelope(
    *,
    chat_kind: ChatKind = ChatKind.DIRECT,
    text: str = "hello",
    reply_to: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        id="qq:qq-main:901",
        connection_id="qq-main",
        platform=Platform.QQ,
        chat_kind=chat_kind,
        chat_id="10001" if chat_kind is ChatKind.DIRECT else "333",
        sender_identity_id="qq:10001",
        occurred_at=datetime(2026, 7, 26, 4, tzinfo=UTC),
        segments=(TextSegment(text=text),),
        reply_to_message_id=reply_to,
    )


def decide(
    env: MessageEnvelope,
    *,
    mentions_self: bool = False,
    replies_to_self: bool = False,
    own_ids: tuple[str, ...] = (),
) -> TurnDecision:
    return decide_turn(
        env,
        mentions_self=mentions_self,
        replies_to_self=replies_to_self,
        own_recent_platform_message_ids=own_ids,
    )


def test_direct_chats_are_always_answered_by_the_agent() -> None:
    decision = decide(envelope())

    assert decision.action is TurnAction.AGENT
    assert decision.trigger is TurnTrigger.DIRECT_MESSAGE
    assert decision.confidence == 1.0


def test_direct_chat_command_prefers_command_trigger() -> None:
    decision = decide(envelope(text="/ping"))

    assert decision.action is TurnAction.DIRECT_REPLY
    assert decision.trigger is TurnTrigger.COMMAND


def test_group_message_without_evidence_is_ignored() -> None:
    decision = decide(envelope(chat_kind=ChatKind.GROUP))

    assert decision.action is TurnAction.IGNORE
    assert decision.reason


def test_group_mention_routes_to_the_agent_with_mention_trigger() -> None:
    decision = decide(envelope(chat_kind=ChatKind.GROUP), mentions_self=True)

    assert decision.action is TurnAction.AGENT
    assert decision.trigger is TurnTrigger.MENTION


def test_group_reply_to_own_message_uses_reply_trigger() -> None:
    via_flag = decide(envelope(chat_kind=ChatKind.GROUP), replies_to_self=True)
    via_recent_ids = decide(
        envelope(chat_kind=ChatKind.GROUP, reply_to="5001"), own_ids=("5001",)
    )
    unrelated_reply = decide(envelope(chat_kind=ChatKind.GROUP, reply_to="404"), own_ids=("5001",))

    assert via_flag.action is TurnAction.AGENT
    assert via_flag.trigger is TurnTrigger.REPLY
    assert via_recent_ids.trigger is TurnTrigger.REPLY
    assert unrelated_reply.action is TurnAction.IGNORE


def test_group_command_replies_and_wins_over_mention() -> None:
    command_only = decide(envelope(chat_kind=ChatKind.GROUP, text="/status"))
    command_and_mention = decide(
        envelope(chat_kind=ChatKind.GROUP, text="/status now"), mentions_self=True
    )

    assert command_only.trigger is TurnTrigger.COMMAND
    assert command_and_mention.trigger is TurnTrigger.COMMAND


def test_channel_behaves_like_group() -> None:
    decision = decide(envelope(chat_kind=ChatKind.CHANNEL))

    assert decision.action is TurnAction.IGNORE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ping", "ping"),
        ("/PING", "ping"),
        ("/status now please", "status"),
        ("hello", None),
        ("/", None),
        ("nothing /ping inline", None),
    ],
)
def test_extract_command_parses_leading_slash_token(text: str, expected: str | None) -> None:
    assert extract_command(envelope(text=text)) == expected


@given(
    chat_kind=st.sampled_from(list(ChatKind)),
    texts=st.lists(st.text(min_size=1, max_size=30).filter(str.strip), min_size=1, max_size=3),
    mentions=st.booleans(),
    replies=st.booleans(),
)
def test_decide_turn_never_raises_and_always_states_a_reason(
    chat_kind: ChatKind, texts: list[str], mentions: bool, replies: bool
) -> None:
    env = MessageEnvelope(
        id="qq:qq-main:1",
        connection_id="qq-main",
        platform=Platform.QQ,
        chat_kind=chat_kind,
        chat_id="1",
        sender_identity_id="qq:2",
        occurred_at=datetime(2026, 7, 26, tzinfo=UTC),
        segments=tuple(TextSegment(text=text) for text in texts),
    )

    decision = decide_turn(env, mentions_self=mentions, replies_to_self=replies)

    assert isinstance(decision, TurnDecision)
    assert decision.reason.strip()
    assert 0.0 <= decision.confidence <= 1.0
