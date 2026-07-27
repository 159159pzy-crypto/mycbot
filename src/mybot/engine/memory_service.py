"""Memory policy: privacy-enforcing retrieval and post-turn extraction."""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast
from uuid import UUID

import structlog
from pydantic import ValidationError

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryMergeDecision,
    MemoryOperation,
    MemoryPrivacy,
    MemoryScope,
    MessageEnvelope,
)
from mybot.engine.prompt import HistoryEntry, estimate_tokens
from mybot.infrastructure.embeddings import EmbeddingError
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.repositories.core_memory import CoreBlockRecord
from mybot.repositories.memory import MemoryRecord, MergeApplication, ScoredMemory

logger = structlog.get_logger("mybot.memory")

_MAX_CANDIDATES = 3
EXTRACTION_SYSTEM_PROMPT = (
    "You extract long-term memories from one chat exchange. Return ONLY a JSON "
    'array, no prose. Each element: {"content": string, "kind": string, '
    '"scope": "subject" | "conversation", "confidence": number between 0 and 1}. '
    'Use scope "subject" for stable facts or preferences about the user '
    'personally, and "conversation" for context that only matters in this '
    "chat. Extract at most 3 items and return [] when nothing is worth "
    "remembering. Never include secrets, credentials, or sensitive personal "
    "data such as health, finances, or government identifiers."
)
MERGE_SYSTEM_PROMPT = (
    "You manage a versioned memory store. Compare one candidate fact with the "
    "same-scope existing facts. Return one JSON object only with operation ADD, "
    "UPDATE, DELETE, or NOOP. ADD requires content. UPDATE requires target_id and "
    "content. DELETE requires target_id. NOOP changes nothing. target_id must be one "
    "of the supplied ids. Prefer NOOP for equivalent wording, UPDATE for a richer or "
    "newer version of the same fact, and DELETE only for an explicit contradiction."
)
FLUSH_SYSTEM_PROMPT = (
    EXTRACTION_SYSTEM_PROMPT
    + " The supplied transcript is about to leave the prompt window. Extract only "
    "facts that remain useful after the immediate exchange; avoid transient assistant text."
)


class MemoryStore(Protocol):
    async def store(
        self,
        item: MemoryItem,
        *,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
    ) -> UUID: ...

    async def search(
        self,
        *,
        query_embedding: Sequence[float],
        query_text: str,
        embedding_model: str,
        subject_identity_id: str,
        conversation_stable_key: str,
        include_private: bool,
        now: datetime,
        limit: int = 5,
    ) -> tuple[ScoredMemory, ...]: ...

    async def similar_for_merge(
        self,
        item: MemoryItem,
        *,
        query_embedding: Sequence[float],
        embedding_model: str,
        now: datetime,
        limit: int = 5,
    ) -> tuple[MemoryRecord, ...]: ...

    async def apply_merge(
        self,
        candidate: MemoryItem,
        decision: MemoryMergeDecision,
        *,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
        source: str,
        now: datetime,
    ) -> MergeApplication: ...

    async def revoke_for(
        self,
        *,
        subject_identity_id: str,
        conversation_stable_key: str | None,
        now: datetime,
        reason: str = "user_forget",
    ) -> int: ...


class Embeddings(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class CoreMemoryStore(Protocol):
    async def visible_for(
        self, subject_identity_id: str
    ) -> tuple[CoreBlockRecord, ...]: ...


class AcquireOnce(Protocol):
    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


class ExtractionLlm(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True, frozen=True)
class ExtractionOutcome:
    stored: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(slots=True)
class MemoryService:
    """Retrieval and extraction under the contract's scope/privacy rules."""

    embeddings: Embeddings
    store: MemoryStore
    llm: ExtractionLlm | None
    embedding_model: str
    core_store: CoreMemoryStore | None = None
    flush_once: AcquireOnce | None = None
    flush_enabled: bool = True
    flush_debounce_ttl_seconds: int = 900
    flush_max_messages: int = 24
    flush_token_budget: int = 2_000
    min_confidence: float = 0.6
    retrieval_limit: int = 5
    token_budget: int = 1_200
    now: Callable[[], datetime] = _utc_now

    async def core_block(self, envelope: MessageEnvelope) -> str | None:
        """Render bounded persona/profile blocks that are always visible this turn."""

        if envelope.ephemeral or self.core_store is None:
            return None
        records = await self.core_store.visible_for(envelope.sender_identity_id)
        sections = [
            f"[{record.block.label.value}]\n{record.block.content.strip()}"
            for record in records
            if record.block.content.strip()
        ]
        if not sections:
            return None
        return "Core memory (durable, operator-visible context):\n\n" + "\n\n".join(
            sections
        )

    async def flush_history(
        self, envelope: MessageEnvelope, history: Sequence[HistoryEntry]
    ) -> ExtractionOutcome:
        """Persist durable facts before bounded prompt history is discarded."""

        if (
            not self.flush_enabled
            or envelope.ephemeral
            or self.llm is None
            or not history
        ):
            return ExtractionOutcome(0, 0, 0)
        stable_key = _conversation_key(envelope).stable_key
        if self.flush_once is not None:
            digest = sha256(stable_key.encode("utf-8")).hexdigest()
            acquired = await self.flush_once.acquire_once(
                f"mybot:memory-flush:{digest}",
                ttl_seconds=self.flush_debounce_ttl_seconds,
            )
            if not acquired:
                return ExtractionOutcome(0, 0, 0)
        selected: list[HistoryEntry] = []
        used_tokens = 0
        for entry in history[: self.flush_max_messages]:
            rendered = f"{entry.direction}:{entry.sender}: {entry.text}"
            cost = estimate_tokens(rendered)
            if used_tokens + cost > self.flush_token_budget:
                break
            used_tokens += cost
            selected.append(entry)
        if not selected:
            return ExtractionOutcome(0, 0, 0)
        transcript = "\n".join(
            f"{entry.direction}:{entry.sender}: {entry.text}"
            for entry in reversed(selected)
        )
        try:
            reply = await self.llm.complete(
                [
                    ChatMessage(role="system", content=FLUSH_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=transcript),
                ]
            )
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning("memory_flush_llm_failed", error=str(error))
            return ExtractionOutcome(0, 0, 0)
        source_ids = tuple(entry.source_id for entry in selected if entry.source_id)
        stored = 0
        prompt_tokens = reply.prompt_tokens
        completion_tokens = reply.completion_tokens
        for candidate in _parse_candidates(reply.text)[:_MAX_CANDIDATES]:
            if candidate.confidence < self.min_confidence:
                continue
            item = self._to_item(
                candidate,
                envelope,
                source_message_ids=source_ids or (envelope.id,),
            )
            applied, usage = await self.merge_item(item, source="pre_truncation_flush")
            prompt_tokens += usage[0]
            completion_tokens += usage[1]
            if (
                applied.operation in {MemoryOperation.ADD, MemoryOperation.UPDATE}
                and applied.applied
            ):
                stored += 1
        return ExtractionOutcome(stored, prompt_tokens, completion_tokens)

    async def retrieval_block(self, envelope: MessageEnvelope, inbound_text: str) -> str | None:
        """Render relevant memories, or None; privacy is enforced here in code."""

        if envelope.ephemeral:
            return None

        try:
            vectors = await self.embeddings.embed([inbound_text])
        except asyncio.CancelledError:
            raise
        except EmbeddingError as error:
            logger.warning("memory_query_embedding_failed", error=str(error))
            return None
        include_private = envelope.chat_kind is ChatKind.DIRECT
        memories = await self.store.search(
            query_embedding=vectors[0],
            query_text=inbound_text,
            embedding_model=self.embedding_model,
            subject_identity_id=envelope.sender_identity_id,
            conversation_stable_key=_conversation_key(envelope).stable_key,
            include_private=include_private,
            now=self.now(),
            limit=self.retrieval_limit,
        )
        if not memories:
            return None
        header = (
            "Relevant remembered facts (use them naturally when helpful; "
            "never claim to know things you do not):"
        )
        lines: list[str] = []
        used = estimate_tokens(header)
        for memory in memories:  # similarity-ordered: keep the best lines
            line = (
                f"- [{memory.scope.value.lower()}|"
                f"{memory.created_at.date().isoformat()}] {memory.content}"
            )
            cost = estimate_tokens(line)
            if used + cost > self.token_budget:
                break
            used += cost
            lines.append(line)
        if not lines:
            return None
        return "\n".join([header, *lines])

    async def extract_and_store(
        self, envelope: MessageEnvelope, reply_text: str
    ) -> ExtractionOutcome:
        """Ask the LLM for candidates and persist the ones that pass policy."""

        if envelope.ephemeral or self.llm is None:
            return ExtractionOutcome(0, 0, 0)
        inbound_text = _first_text(envelope)
        messages = [
            ChatMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    f"User ({envelope.sender_identity_id}): {inbound_text}\nAssistant: {reply_text}"
                ),
            ),
        ]
        try:
            reply = await self.llm.complete(messages)
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning("memory_extraction_llm_failed", error=str(error))
            return ExtractionOutcome(0, 0, 0)
        stored = 0
        prompt_tokens = reply.prompt_tokens
        completion_tokens = reply.completion_tokens
        for candidate in _parse_candidates(reply.text)[:_MAX_CANDIDATES]:
            if candidate.confidence < self.min_confidence:
                continue
            item = self._to_item(candidate, envelope)
            applied, usage = await self.merge_item(item, source="post_turn")
            prompt_tokens += usage[0]
            completion_tokens += usage[1]
            if (
                applied.operation in {MemoryOperation.ADD, MemoryOperation.UPDATE}
                and applied.applied
            ):
                stored += 1
        if stored:
            logger.info("memories_stored", count=stored)
        return ExtractionOutcome(stored, prompt_tokens, completion_tokens)

    async def merge_item(
        self, item: MemoryItem, *, source: str
    ) -> tuple[MergeApplication, tuple[int, int]]:
        """Embed, compare, decide, and atomically apply one candidate memory."""

        try:
            vectors = await self.embeddings.embed([item.content])
        except asyncio.CancelledError:
            raise
        except EmbeddingError as error:
            logger.warning("memory_store_embedding_failed", error=str(error))
            return (
                MergeApplication(MemoryOperation.NOOP, None, None, False),
                (0, 0),
            )
        similar = await self.store.similar_for_merge(
            item,
            query_embedding=vectors[0],
            embedding_model=self.embedding_model,
            now=self.now(),
        )
        decision = MemoryMergeDecision(
            operation=MemoryOperation.ADD,
            content=item.content,
            kind=item.kind,
            confidence=item.confidence,
        )
        usage = (0, 0)
        if similar and self.llm is not None:
            messages = [
                ChatMessage(role="system", content=MERGE_SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "candidate": {
                                "content": item.content,
                                "kind": item.kind,
                                "confidence": item.confidence,
                            },
                            "existing": [
                                {
                                    "id": str(memory.id),
                                    "content": memory.content,
                                    "kind": memory.kind,
                                    "confidence": memory.confidence,
                                }
                                for memory in similar
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ]
            try:
                reply = await self.llm.complete(messages)
            except asyncio.CancelledError:
                raise
            except LlmError as error:
                logger.warning("memory_merge_llm_failed", error=str(error))
                decision = MemoryMergeDecision(operation=MemoryOperation.NOOP)
            else:
                usage = (reply.prompt_tokens, reply.completion_tokens)
                decision = _parse_merge_decision(
                    reply.text,
                    allowed_ids={memory.id for memory in similar},
                    candidate=item,
                )
        applied = await self.store.apply_merge(
            item,
            decision,
            embedding=vectors[0],
            embedding_model=self.embedding_model,
            source=source,
            now=self.now(),
        )
        return applied, usage

    async def forget(self, *, subject_identity_id: str, conversation_stable_key: str | None) -> int:
        return await self.store.revoke_for(
            subject_identity_id=subject_identity_id,
            conversation_stable_key=conversation_stable_key,
            now=self.now(),
        )

    def _to_item(
        self,
        candidate: "_Candidate",
        envelope: MessageEnvelope,
        *,
        source_message_ids: tuple[str, ...] | None = None,
    ) -> MemoryItem:
        # Extraction can never produce SENSITIVE items; that privacy level is
        # reserved for explicit operator/user action in a later milestone.
        if candidate.scope == "subject":
            return MemoryItem(
                scope=MemoryScope.SUBJECT,
                subject_identity_id=envelope.sender_identity_id,
                kind=candidate.kind,
                content=candidate.content,
                source_message_ids=source_message_ids or (envelope.id,),
                confidence=candidate.confidence,
                privacy=MemoryPrivacy.PRIVATE,
            )
        return MemoryItem(
            scope=MemoryScope.CONVERSATION,
            conversation=_conversation_key(envelope),
            kind=candidate.kind,
            content=candidate.content,
            source_message_ids=source_message_ids or (envelope.id,),
            confidence=candidate.confidence,
            privacy=MemoryPrivacy.SHARED,
        )


@dataclass(slots=True, frozen=True)
class _Candidate:
    content: str
    kind: str
    scope: str
    confidence: float


def _conversation_key(envelope: MessageEnvelope) -> ConversationKey:
    return ConversationKey(
        connection_id=envelope.connection_id,
        chat_kind=envelope.chat_kind,
        chat_id=envelope.chat_id,
        thread_id=envelope.thread_id,
    )


def _first_text(envelope: MessageEnvelope) -> str:
    from mybot.engine.prompt import envelope_prompt

    return envelope_prompt(envelope, include_images=False).summary


def _parse_candidates(text: str) -> list[_Candidate]:
    decoded = _decode_json_array(text)
    candidates: list[_Candidate] = []
    for raw in decoded:
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, object], raw)
        content = entry.get("content")
        confidence = entry.get("confidence")
        if not isinstance(content, str) or not content.strip():
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            continue
        kind = entry.get("kind")
        scope = entry.get("scope")
        candidates.append(
            _Candidate(
                content=content.strip(),
                kind=kind.strip() if isinstance(kind, str) and kind.strip() else "fact",
                scope="subject" if scope == "subject" else "conversation",
                confidence=max(0.0, min(1.0, float(confidence))),
            )
        )
    return candidates


def _parse_merge_decision(
    text: str, *, allowed_ids: set[UUID], candidate: MemoryItem
) -> MemoryMergeDecision:
    decoded = _decode_json_object(text)
    raw_operation = decoded.get("operation", decoded.get("event", "NOOP"))
    operation_name = str(raw_operation).upper()
    if operation_name == "NONE":
        operation_name = "NOOP"
    try:
        operation = MemoryOperation(operation_name)
    except ValueError:
        return MemoryMergeDecision(operation=MemoryOperation.NOOP)

    target_id: UUID | None = None
    raw_target = decoded.get("target_id", decoded.get("id"))
    if raw_target is not None:
        try:
            parsed_target = UUID(str(raw_target))
        except ValueError:
            return MemoryMergeDecision(operation=MemoryOperation.NOOP)
        if parsed_target not in allowed_ids:
            return MemoryMergeDecision(operation=MemoryOperation.NOOP)
        target_id = parsed_target

    content = decoded.get("content", decoded.get("text"))
    resolved_content = content.strip() if isinstance(content, str) and content.strip() else None
    kind = decoded.get("kind")
    confidence = decoded.get("confidence")
    payload: dict[str, object] = {
        "operation": operation,
        "target_id": target_id,
        "content": resolved_content,
        "kind": kind.strip() if isinstance(kind, str) and kind.strip() else candidate.kind,
        "confidence": (
            float(confidence)
            if isinstance(confidence, int | float) and not isinstance(confidence, bool)
            else candidate.confidence
        ),
    }
    if operation is MemoryOperation.ADD:
        payload["target_id"] = None
        payload["content"] = resolved_content or candidate.content
    elif operation is MemoryOperation.UPDATE and resolved_content is None:
        payload["content"] = candidate.content
    elif operation in {MemoryOperation.DELETE, MemoryOperation.NOOP}:
        payload["content"] = None
        payload["kind"] = None
        payload["confidence"] = None
    try:
        return MemoryMergeDecision.model_validate(payload)
    except ValidationError:
        return MemoryMergeDecision(operation=MemoryOperation.NOOP)


def _decode_json_array(text: str) -> list[object]:
    for chunk in (text, text[text.find("[") : text.rfind("]") + 1]):
        if not chunk:
            continue
        try:
            decoded = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(decoded, list):
            return cast(list[object], decoded)
    return []


def _decode_json_object(text: str) -> dict[str, object]:
    start = text.find("{")
    end = text.rfind("}")
    for chunk in (text, text[start : end + 1]):
        if not chunk:
            continue
        try:
            decoded = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            return cast(dict[str, object], decoded)
    return {}
