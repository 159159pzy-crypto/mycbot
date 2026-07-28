from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from mybot.contracts import ChatKind, MessageEnvelope, Platform, TextSegment
from mybot.security.pairing import PairingGate, PairingPolicy, PairingRequest


@dataclass
class FakeStore:
    mode: str = "paired"
    is_approved: bool = False
    created: bool = True

    async def policy(self, platform: str, connection_id: str) -> PairingPolicy:
        allowlist = ("qq:trusted",) if self.mode == "allowlist" else ()
        return PairingPolicy(self.mode, allowlist)

    async def approved(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> bool:
        return self.is_approved

    async def request(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> tuple[PairingRequest | None, bool]:
        return (
            PairingRequest(
                id="request-1",
                platform=platform,
                connection_id=connection_id,
                subject_identity_id=subject_identity_id,
                code="ABCD2345",
                expires_at="2026-07-28T13:00:00+00:00",
            ),
            self.created,
        )


def envelope(*, chat_kind: ChatKind = ChatKind.DIRECT, subject: str = "qq:unknown"):
    return MessageEnvelope(
        id="qq:1",
        connection_id="qq-main",
        platform=Platform.QQ,
        chat_kind=chat_kind,
        chat_id="1",
        sender_identity_id=subject,
        occurred_at=datetime(2026, 7, 28, tzinfo=UTC),
        segments=(TextSegment(text="hello"),),
    )


@pytest.mark.asyncio
async def test_pairing_gate_only_prompts_for_new_unknown_direct_requests() -> None:
    store = FakeStore()
    gate = PairingGate(store)
    first = await gate.check(envelope())
    assert first.allowed is False
    assert "ABCD2345" in (first.reply_text or "")

    store.created = False
    repeated = await gate.check(envelope())
    assert repeated.allowed is False
    assert repeated.reply_text is None

    store.is_approved = True
    assert (await gate.check(envelope())).allowed is True
    assert (await gate.check(envelope(chat_kind=ChatKind.GROUP))).allowed is True


@pytest.mark.asyncio
async def test_open_and_allowlist_policies_do_not_consume_pairing_approvals() -> None:
    store = FakeStore(mode="open")
    assert (await PairingGate(store).check(envelope())).allowed is True
    store.mode = "allowlist"
    assert (await PairingGate(store).check(envelope(subject="qq:trusted"))).allowed is True
    denied = await PairingGate(store).check(envelope())
    assert denied.allowed is False
    assert denied.reason == "allowlist_denied"
