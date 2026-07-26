"""Platform adapters translating between platform payloads and public contracts."""

from uuid import UUID

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.messages import ChatKind, MessageEnvelope, Platform
from mybot.contracts.replies import ReplyPlan


class InboundEvent(FrozenModel):
    """A translated inbound message plus gateway-detected addressing evidence."""

    envelope: MessageEnvelope
    mentions_self: bool = False
    replies_to_self: bool = False
    sender_is_bot: bool = False


class OutboundMessage(FrozenModel):
    """A reply routed from a worker back to the owning platform connection."""

    internal_message_id: UUID
    platform: Platform
    connection_id: NonEmptyStr
    chat_kind: ChatKind
    chat_id: NonEmptyStr
    reply_plan: ReplyPlan
    reply_to_platform_message_id: NonEmptyStr | None = None


__all__ = ["InboundEvent", "OutboundMessage"]
