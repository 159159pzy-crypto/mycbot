"""Rule-based turn routing for the messaging spine milestone."""

from collections.abc import Sequence

from mybot.contracts import (
    ChatKind,
    MessageEnvelope,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)


def extract_command(envelope: MessageEnvelope) -> str | None:
    """Return the lowered leading slash-command token, if the message starts with one."""

    for segment in envelope.segments:
        if isinstance(segment, TextSegment):
            text = segment.text.strip()
            if not text.startswith("/"):
                return None
            token = text.split(maxsplit=1)[0][1:]
            return token.lower() or None
    return None


def decide_turn(
    envelope: MessageEnvelope,
    *,
    mentions_self: bool,
    replies_to_self: bool,
    own_recent_platform_message_ids: Sequence[str] = (),
) -> TurnDecision:
    """Route one inbound message: DMs always answer, groups need addressing evidence."""

    command = extract_command(envelope)
    replied_to_own = replies_to_self or (
        envelope.reply_to_message_id is not None
        and envelope.reply_to_message_id in own_recent_platform_message_ids
    )

    if envelope.chat_kind is ChatKind.DIRECT:
        if command is not None:
            return TurnDecision(
                action=TurnAction.DIRECT_REPLY,
                reason=f"direct chat command /{command}",
                confidence=1.0,
                trigger=TurnTrigger.COMMAND,
            )
        return TurnDecision(
            action=TurnAction.AGENT,
            reason="direct chats are always answered by the agent",
            confidence=1.0,
            trigger=TurnTrigger.DIRECT_MESSAGE,
        )

    if command is not None:
        return TurnDecision(
            action=TurnAction.DIRECT_REPLY,
            reason=f"group command /{command}",
            confidence=1.0,
            trigger=TurnTrigger.COMMAND,
        )
    if mentions_self:
        return TurnDecision(
            action=TurnAction.AGENT,
            reason="the bot was mentioned",
            confidence=1.0,
            trigger=TurnTrigger.MENTION,
        )
    if replied_to_own:
        return TurnDecision(
            action=TurnAction.AGENT,
            reason="the message replies to one of the bot's messages",
            confidence=1.0,
            trigger=TurnTrigger.REPLY,
        )
    return TurnDecision(
        action=TurnAction.IGNORE,
        reason="group message without mention, reply, or command",
        confidence=1.0,
        trigger=TurnTrigger.POLICY,
    )
