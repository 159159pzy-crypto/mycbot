"""Bounded group-only learning for local expression and subject relationships."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast

import structlog

from mybot.contracts import MemoryItem, MemoryPrivacy, MemoryScope
from mybot.engine.memory_service import ExtractionLlm
from mybot.engine.prompt import estimate_tokens
from mybot.infrastructure.llm import ChatMessage, LlmError
from mybot.repositories.memory import MemoryRecord, MergeApplication
from mybot.repositories.messages import GroupLearningContext, MessageText

logger = structlog.get_logger("mybot.personality_learning")

LEARNING_SYSTEM_PROMPT = (
    "Analyze group dialogue for two safe, bounded outputs. Return one JSON object only: "
    '{"expressions":[{"content":string,"confidence":0..1}],'
    '"relationships":[{"subject_identity_id":string,"impression":string,'
    '"familiarity_delta":0..5}]}. Expressions are short reusable rhythm or tone patterns, '
    "not personal facts and not verbatim private quotes. Relationships are neutral, "
    "non-sensitive interaction impressions for supplied sender ids only. Never infer health, "
    "finance, identity documents, secrets, protected traits, or off-chat facts. Return empty "
    "arrays when evidence is weak. At most 3 expressions and 5 relationships."
)


class GroupLearningSource(Protocol):
    async def recent_group_learning_contexts(
        self,
        *,
        since: datetime,
        conversation_limit: int,
        message_limit: int,
    ) -> tuple[GroupLearningContext, ...]: ...


class MemoryMerger(Protocol):
    async def merge_item(
        self, item: MemoryItem, *, source: str
    ) -> tuple[MergeApplication, tuple[int, int]]: ...


class RelationshipStore(Protocol):
    async def relationship_for(
        self, subject_identity_id: str, *, now: datetime
    ) -> MemoryRecord | None: ...

    async def upsert_relationship(
        self, item: MemoryItem, *, source: str, now: datetime
    ) -> MergeApplication: ...


class AcquireOnce(Protocol):
    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class PersonalityLearningJob:
    source: GroupLearningSource
    memory: MemoryMerger
    relationships: RelationshipStore
    llm: ExtractionLlm
    once: AcquireOnce
    enabled: bool = False
    lookback_hours: int = 24
    conversation_limit: int = 10
    message_limit: int = 80
    prompt_token_budget: int = 3_000
    min_confidence: float = 0.7
    dedupe_ttl_seconds: int = 86_400
    now: Callable[[], datetime] = field(default=_utc_now)

    async def run_pass(self) -> int:
        if not self.enabled:
            return 0
        current = self.now()
        contexts = await self.source.recent_group_learning_contexts(
            since=current - timedelta(hours=self.lookback_hours),
            conversation_limit=self.conversation_limit,
            message_limit=self.message_limit,
        )
        changed = 0
        for context in contexts:
            if not context.source_message_ids:
                continue
            fingerprint = sha256(
                "\0".join(context.source_message_ids).encode("utf-8")
            ).hexdigest()
            if not await self.once.acquire_once(
                f"mybot:personality-learning:{context.conversation.stable_key}:{fingerprint}",
                ttl_seconds=self.dedupe_ttl_seconds,
            ):
                continue
            try:
                changed += await self._learn_context(context, current)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "personality_learning_context_failed",
                    conversation=context.conversation.stable_key,
                )
        if changed:
            logger.info("personality_learning_pass_completed", changed=changed)
        return changed

    async def _learn_context(
        self, context: GroupLearningContext, current: datetime
    ) -> int:
        transcript = _bounded_transcript(context, self.prompt_token_budget)
        if len(transcript) < 3:
            return 0
        payload = {
            "conversation": context.conversation.stable_key,
            "messages": [
                {"sender": message.sender, "text": message.text} for message in transcript
            ],
        }
        try:
            reply = await self.llm.complete(
                [
                    ChatMessage(role="system", content=LEARNING_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=json.dumps(payload, ensure_ascii=False),
                    ),
                ]
            )
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning("personality_learning_llm_failed", error=str(error))
            return 0
        parsed = _decode_object(reply.text)
        changed = 0
        source_ids = context.source_message_ids
        for expression in _object_list(parsed.get("expressions"))[:3]:
            content = _short_text(expression.get("content"), limit=200)
            confidence = _number(expression.get("confidence"), low=0.0, high=1.0)
            if not content or confidence < self.min_confidence:
                continue
            item = MemoryItem(
                scope=MemoryScope.CONVERSATION,
                conversation=context.conversation,
                kind="EXPRESSION",
                content=content,
                source_message_ids=source_ids,
                confidence=confidence,
                privacy=MemoryPrivacy.SHARED,
            )
            applied, _ = await self.memory.merge_item(item, source="expression_learning")
            changed += int(applied.applied)

        allowed_senders = {message.sender for message in transcript}
        sender_sources: dict[str, list[str]] = {}
        for message in transcript:
            if message.id is not None:
                sender_sources.setdefault(message.sender, []).append(str(message.id))
        for relation in _object_list(parsed.get("relationships"))[:5]:
            subject = _short_text(relation.get("subject_identity_id"), limit=300)
            impression = _short_text(relation.get("impression"), limit=500)
            delta = _number(relation.get("familiarity_delta"), low=0.0, high=5.0)
            if not subject or subject not in allowed_senders or not impression:
                continue
            previous = await self.relationships.relationship_for(subject, now=current)
            previous_score = previous.relationship_score if previous is not None else 0.0
            familiarity = min(100.0, max(0.0, (previous_score or 0.0) + delta))
            sources = tuple(sender_sources.get(subject, [])) or source_ids
            item = MemoryItem(
                scope=MemoryScope.SUBJECT,
                subject_identity_id=subject,
                kind="RELATIONSHIP",
                content=impression,
                source_message_ids=sources,
                confidence=0.85,
                relationship_score=familiarity,
                privacy=MemoryPrivacy.PRIVATE,
            )
            applied = await self.relationships.upsert_relationship(
                item,
                source="relationship_learning",
                now=current,
            )
            changed += int(applied.applied)
        return changed


def _bounded_transcript(
    context: GroupLearningContext, token_budget: int
) -> tuple[MessageText, ...]:
    selected: list[MessageText] = []
    used = estimate_tokens(LEARNING_SYSTEM_PROMPT)
    for message in reversed(context.messages):
        cost = estimate_tokens(f"{message.sender}: {message.text}")
        if used + cost > token_budget:
            continue
        selected.append(message)
        used += cost
    selected.reverse()
    return tuple(selected)


def _decode_object(text: str) -> dict[str, object]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        cast(dict[str, object], item)
        for item in cast(list[object], value)
        if isinstance(item, dict)
    ]


def _short_text(value: object, *, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _number(value: object, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(high, max(low, float(value)))
