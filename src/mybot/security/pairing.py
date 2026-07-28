"""Direct-message access decisions and one-time pairing prompts."""

from dataclasses import dataclass
from typing import Protocol

from mybot.contracts import ChatKind, MessageEnvelope


@dataclass(slots=True, frozen=True)
class PairingPolicy:
    policy: str
    allowlist: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PairingRequest:
    id: str
    platform: str
    connection_id: str
    subject_identity_id: str
    code: str
    expires_at: str


@dataclass(slots=True, frozen=True)
class PairingDecision:
    allowed: bool
    reply_text: str | None = None
    reason: str = "allowed"


class PairingStore(Protocol):
    async def policy(self, platform: str, connection_id: str) -> PairingPolicy: ...

    async def approved(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> bool: ...

    async def request(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> tuple[PairingRequest | None, bool]: ...


@dataclass(slots=True)
class PairingGate:
    store: PairingStore

    async def check(self, envelope: MessageEnvelope) -> PairingDecision:
        if envelope.chat_kind is not ChatKind.DIRECT:
            return PairingDecision(True)
        platform = envelope.platform.value
        policy = await self.store.policy(platform, envelope.connection_id)
        if policy.policy == "open":
            return PairingDecision(True)
        if policy.policy == "allowlist":
            allowed = envelope.sender_identity_id in set(policy.allowlist)
            return PairingDecision(allowed, reason="allowlist_denied" if not allowed else "allowed")
        if policy.policy != "paired":
            return PairingDecision(False, reason="invalid_policy")
        if await self.store.approved(
            platform, envelope.connection_id, envelope.sender_identity_id
        ):
            return PairingDecision(True)
        request, created = await self.store.request(
            platform, envelope.connection_id, envelope.sender_identity_id
        )
        if request is None or not created:
            return PairingDecision(False, reason="pairing_pending")
        return PairingDecision(
            False,
            reply_text=(
                f"此账号需要先完成配对。配对码: {request.code} (1 小时内有效)。"
                "请让管理员在 MyBot 控制台中批准此请求。"
            ),
            reason="pairing_required",
        )


__all__ = [
    "PairingDecision",
    "PairingGate",
    "PairingPolicy",
    "PairingRequest",
]
