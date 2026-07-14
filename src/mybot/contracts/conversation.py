"""Stable conversation identity and turn routing contracts."""

from enum import StrEnum
from urllib.parse import quote

from pydantic import Field

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.messages import ChatKind


class ConversationKey(FrozenModel):
    connection_id: NonEmptyStr
    chat_kind: ChatKind
    chat_id: NonEmptyStr
    thread_id: NonEmptyStr | None = None

    @property
    def stable_key(self) -> str:
        parts = (
            "v1",
            quote(self.connection_id, safe=""),
            self.chat_kind.value,
            quote(self.chat_id, safe=""),
        )
        if self.thread_id is None:
            return ":".join((*parts, "0"))
        return ":".join((*parts, "1", quote(self.thread_id, safe="")))

    def __str__(self) -> str:
        return self.stable_key


class TurnAction(StrEnum):
    IGNORE = "IGNORE"
    DIRECT_REPLY = "DIRECT_REPLY"
    AGENT = "AGENT"


class TurnTrigger(StrEnum):
    DIRECT_MESSAGE = "DIRECT_MESSAGE"
    MENTION = "MENTION"
    REPLY = "REPLY"
    COMMAND = "COMMAND"
    PROACTIVE = "PROACTIVE"
    POLICY = "POLICY"


class TurnDecision(FrozenModel):
    action: TurnAction
    reason: NonEmptyStr
    confidence: float = Field(ge=0.0, le=1.0)
    trigger: TurnTrigger
