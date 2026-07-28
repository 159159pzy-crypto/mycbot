"""Deterministic, auditable participation scoring for unaddressed group messages."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from mybot.contracts import (
    MessageEnvelope,
    ReplyWillingnessPolicy,
    WillingnessComponents,
    WillingnessScore,
)
from mybot.engine.prompt import envelope_text

_QUESTION_TERMS = ("怎么", "如何", "为什么", "有没有", "怎么办", "谁知道")
_REQUEST_TERMS = ("帮忙", "求助", "建议", "看看", "能不能")


@dataclass(slots=True, frozen=True)
class ActivitySnapshot:
    inbound: int = 0
    outbound: int = 0

    @property
    def total(self) -> int:
        return self.inbound + self.outbound


class ActivitySource(Protocol):
    async def recent_activity(
        self, conversation_id: UUID, *, since: datetime, limit: int = 200
    ) -> ActivitySnapshot: ...


class SemanticRelevance(Protocol):
    async def participation_relevance(
        self, envelope: MessageEnvelope, text: str, persona: str
    ) -> tuple[float, float]: ...


class WillingnessAudit(Protocol):
    async def record(
        self,
        *,
        conversation_id: UUID,
        message_id: UUID | None,
        profile_id: UUID | None,
        score: WillingnessScore,
    ) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class ReplyWillingnessScorer:
    activity: ActivitySource
    semantic: SemanticRelevance
    audit: WillingnessAudit
    activity_window_minutes: int = 10
    now: Callable[[], datetime] = field(default=_utc_now)

    async def evaluate(
        self,
        *,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        message_id: UUID | None,
        profile_id: UUID | None,
        persona: str,
        policy: ReplyWillingnessPolicy,
    ) -> WillingnessScore:
        threshold = _clamp(policy.threshold / policy.sensitivity)
        if not policy.enabled:
            score = WillingnessScore(
                score=0.0,
                threshold=threshold,
                allowed=False,
                reason="profile willingness disabled",
                components=WillingnessComponents(),
            )
            await self._audit(conversation_id, message_id, profile_id, score)
            return score

        text = envelope_text(envelope).strip()
        keyword = _keyword_score(text, policy.keywords)
        question = _question_score(text)
        try:
            persona_relevance, memory_relevance = await self.semantic.participation_relevance(
                envelope, text, persona
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            score = WillingnessScore(
                score=0.0,
                threshold=threshold,
                allowed=False,
                reason="semantic relevance unavailable",
                components=WillingnessComponents(keyword=keyword, question=question),
            )
            await self._audit(conversation_id, message_id, profile_id, score)
            return score

        snapshot = await self.activity.recent_activity(
            conversation_id,
            since=self.now() - timedelta(minutes=self.activity_window_minutes),
        )
        group_heat = _clamp(snapshot.inbound / 20)
        presence_penalty = (
            _clamp(snapshot.outbound / snapshot.total) if snapshot.total else 0.0
        )
        components = WillingnessComponents(
            keyword=keyword,
            question=question,
            persona_relevance=_clamp(persona_relevance),
            memory_relevance=_clamp(memory_relevance),
            group_heat=group_heat,
            presence_penalty=presence_penalty,
        )
        raw = (
            0.28 * components.keyword
            + 0.22 * components.question
            + 0.14 * components.persona_relevance
            + 0.22 * components.memory_relevance
            + 0.14 * components.group_heat
            - 0.30 * components.presence_penalty
        )
        final = round(_clamp(raw), 4)
        allowed = final >= threshold
        reasons = [
            name
            for name, value in (
                ("keyword", components.keyword),
                ("question", components.question),
                ("persona", components.persona_relevance),
                ("memory", components.memory_relevance),
                ("heat", components.group_heat),
            )
            if value >= 0.5
        ]
        score = WillingnessScore(
            score=final,
            threshold=round(threshold, 4),
            allowed=allowed,
            reason=(" + ".join(reasons) if reasons else "weak group relevance"),
            components=components,
        )
        await self._audit(conversation_id, message_id, profile_id, score)
        return score

    async def _audit(
        self,
        conversation_id: UUID,
        message_id: UUID | None,
        profile_id: UUID | None,
        score: WillingnessScore,
    ) -> None:
        await self.audit.record(
            conversation_id=conversation_id,
            message_id=message_id,
            profile_id=profile_id,
            score=score,
        )


def _keyword_score(text: str, keywords: tuple[str, ...]) -> float:
    normalized = text.casefold()
    return 1.0 if any(keyword.casefold() in normalized for keyword in keywords) else 0.0


def _question_score(text: str) -> float:
    if not text:
        return 0.0
    if any(term in text for term in (*_QUESTION_TERMS, *_REQUEST_TERMS)):
        return 1.0
    if re.search("[?\\uFF1F](?:\\s|$)", text):
        return 0.8
    return 0.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
