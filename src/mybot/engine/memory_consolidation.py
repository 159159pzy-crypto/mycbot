"""Low-frequency reflection that feeds candidates into the normal merge pipeline."""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import structlog

from mybot.contracts import MemoryItem, MemoryPrivacy, MemoryScope
from mybot.engine.prompt import estimate_tokens
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.repositories.memory import ConsolidationContext, MergeApplication

logger = structlog.get_logger("mybot.memory_consolidation")

CONSOLIDATION_SYSTEM_PROMPT = (
    "Review bounded recent dialogue and active memories. Return ONLY a JSON array "
    "with at most 5 durable candidate objects: content, kind, scope "
    "(conversation|subject|global), confidence. Produce richer rewrites, promotions, "
    "or durable insights; omit duplicates and transient details. GLOBAL is allowed "
    "only for non-personal, non-sensitive facts safe in every chat. Never include "
    "credentials, health, finance, government identifiers, or private third-party data."
)


class ConsolidationSource(Protocol):
    async def recent_consolidation_contexts(
        self,
        *,
        since: datetime,
        now: datetime,
        conversation_limit: int,
        message_limit: int,
        memory_limit: int,
    ) -> tuple[ConsolidationContext, ...]: ...


class AcquireOnce(Protocol):
    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


class ConsolidationLlm(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


class MergeMemory(Protocol):
    async def merge_item(
        self, item: MemoryItem, *, source: str
    ) -> tuple[MergeApplication, tuple[int, int]]: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class MemoryConsolidationJob:
    source: ConsolidationSource
    memory: MergeMemory
    llm: ConsolidationLlm | None
    once: AcquireOnce | None
    interval_seconds: int = 86_400
    lookback_hours: int = 48
    conversation_limit: int = 20
    message_limit: int = 40
    memory_limit: int = 20
    prompt_token_budget: int = 4_000
    min_confidence: float = 0.6
    enabled: bool = True
    now: Callable[[], datetime] = field(default=_utc_now)

    async def run_pass(self) -> int:
        if not self.enabled or self.llm is None:
            return 0
        now = self.now()
        if self.once is not None:
            bucket = int(now.timestamp()) // self.interval_seconds
            acquired = await self.once.acquire_once(
                f"mybot:memory-consolidation:{bucket}",
                ttl_seconds=self.interval_seconds,
            )
            if not acquired:
                return 0
        contexts = await self.source.recent_consolidation_contexts(
            since=now - timedelta(hours=self.lookback_hours),
            now=now,
            conversation_limit=self.conversation_limit,
            message_limit=self.message_limit,
            memory_limit=self.memory_limit,
        )
        applied = 0
        for context in contexts:
            applied += await self._consolidate(context)
        logger.info(
            "memory_consolidation_pass_completed",
            contexts=len(contexts),
            applied=applied,
        )
        return applied

    async def _consolidate(self, context: ConsolidationContext) -> int:
        payload = _bounded_payload(context, self.prompt_token_budget)
        if payload is None or self.llm is None:
            return 0
        try:
            reply = await self.llm.complete(
                [
                    ChatMessage(role="system", content=CONSOLIDATION_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=payload),
                ]
            )
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning("memory_consolidation_llm_failed", error=str(error))
            return 0
        applied = 0
        for candidate in _parse_candidates(reply.text)[:5]:
            if candidate.confidence < self.min_confidence:
                continue
            item = _to_item(candidate, context)
            outcome, _ = await self.memory.merge_item(
                item,
                source="sleep_consolidation",
            )
            if outcome.applied:
                applied += 1
        return applied


@dataclass(slots=True, frozen=True)
class _Candidate:
    content: str
    kind: str
    scope: MemoryScope
    confidence: float


def _bounded_payload(context: ConsolidationContext, token_budget: int) -> str | None:
    payload: dict[str, object] = {
        "conversation": context.conversation.stable_key,
        "subject": context.subject_identity_id,
        "messages": [],
        "active_memories": [],
    }
    used = estimate_tokens(json.dumps(payload, ensure_ascii=False))
    messages: list[str] = []
    for message in reversed(context.transcript):
        cost = estimate_tokens(message)
        if used + cost > token_budget:
            break
        used += cost
        messages.append(message)
    messages.reverse()
    memories: list[dict[str, object]] = []
    for memory in context.memories:
        rendered: dict[str, object] = {
            "id": str(memory.id),
            "scope": memory.scope.value,
            "kind": memory.kind,
            "content": memory.content,
        }
        cost = estimate_tokens(json.dumps(rendered, ensure_ascii=False))
        if used + cost > token_budget:
            break
        used += cost
        memories.append(rendered)
    if not messages:
        return None
    payload["messages"] = messages
    payload["active_memories"] = memories
    return json.dumps(payload, ensure_ascii=False)


def _parse_candidates(text: str) -> list[_Candidate]:
    start = text.find("[")
    end = text.rfind("]")
    for chunk in (text, text[start : end + 1]):
        if not chunk:
            continue
        try:
            decoded = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(decoded, list):
            continue
        candidates: list[_Candidate] = []
        for raw in cast(list[object], decoded):
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, object], raw)
            content = item.get("content")
            confidence = item.get("confidence")
            if not isinstance(content, str) or not content.strip():
                continue
            if isinstance(confidence, bool) or not isinstance(confidence, int | float):
                continue
            raw_scope = str(item.get("scope", "conversation")).upper()
            try:
                scope = MemoryScope(raw_scope)
            except ValueError:
                continue
            kind = item.get("kind")
            candidates.append(
                _Candidate(
                    content=content.strip(),
                    kind=kind.strip() if isinstance(kind, str) and kind.strip() else "insight",
                    scope=scope,
                    confidence=max(0.0, min(1.0, float(confidence))),
                )
            )
        return candidates
    return []


def _to_item(candidate: _Candidate, context: ConsolidationContext) -> MemoryItem:
    if candidate.scope is MemoryScope.GLOBAL:
        return MemoryItem(
            scope=MemoryScope.GLOBAL,
            kind=candidate.kind,
            content=candidate.content,
            source_message_ids=context.source_message_ids,
            confidence=candidate.confidence,
            privacy=MemoryPrivacy.PUBLIC,
        )
    if candidate.scope is MemoryScope.SUBJECT:
        return MemoryItem(
            scope=MemoryScope.SUBJECT,
            subject_identity_id=context.subject_identity_id,
            kind=candidate.kind,
            content=candidate.content,
            source_message_ids=context.source_message_ids,
            confidence=candidate.confidence,
            privacy=MemoryPrivacy.PRIVATE,
        )
    return MemoryItem(
        scope=MemoryScope.CONVERSATION,
        conversation=context.conversation,
        kind=candidate.kind,
        content=candidate.content,
        source_message_ids=context.source_message_ids,
        confidence=candidate.confidence,
        privacy=MemoryPrivacy.SHARED,
    )


__all__ = ["MemoryConsolidationJob"]
