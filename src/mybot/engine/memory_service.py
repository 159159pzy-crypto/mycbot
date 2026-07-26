"""Memory policy: privacy-enforcing retrieval and post-turn extraction."""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

import structlog

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    MessageEnvelope,
)
from mybot.engine.prompt import estimate_tokens
from mybot.infrastructure.embeddings import EmbeddingClient, EmbeddingError
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.repositories.memory import ScoredMemory

logger = structlog.get_logger("mybot.memory")

_MAX_CANDIDATES = 3
EXTRACTION_SYSTEM_PROMPT = (
    "You extract long-term memories from one chat exchange. Return ONLY a JSON "
    'array, no prose. Each element: {"content": string, "kind": string, '
    '"scope": "subject" | "conversation", "confidence": number between 0 and 1}. '
    "Use scope \"subject\" for stable facts or preferences about the user "
    "personally, and \"conversation\" for context that only matters in this "
    "chat. Extract at most 3 items and return [] when nothing is worth "
    "remembering. Never include secrets, credentials, or sensitive personal "
    "data such as health, finances, or government identifiers."
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
        embedding_model: str,
        subject_identity_id: str,
        conversation_stable_key: str,
        include_private: bool,
        now: datetime,
        limit: int = 5,
    ) -> tuple[ScoredMemory, ...]: ...

    async def revoke_for(
        self,
        *,
        subject_identity_id: str,
        conversation_stable_key: str | None,
        now: datetime,
        reason: str = "user_forget",
    ) -> int: ...


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

    embeddings: EmbeddingClient
    store: MemoryStore
    llm: ExtractionLlm | None
    embedding_model: str
    min_confidence: float = 0.6
    retrieval_limit: int = 5
    token_budget: int = 1_200
    now: Callable[[], datetime] = _utc_now

    async def retrieval_block(
        self, envelope: MessageEnvelope, inbound_text: str
    ) -> str | None:
        """Render relevant memories, or None; privacy is enforced here in code."""

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

        if self.llm is None:
            return ExtractionOutcome(0, 0, 0)
        inbound_text = _first_text(envelope)
        messages = [
            ChatMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    f"User ({envelope.sender_identity_id}): {inbound_text}\n"
                    f"Assistant: {reply_text}"
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
        for candidate in _parse_candidates(reply.text)[:_MAX_CANDIDATES]:
            if candidate.confidence < self.min_confidence:
                continue
            item = self._to_item(candidate, envelope)
            try:
                vectors = await self.embeddings.embed([item.content])
            except asyncio.CancelledError:
                raise
            except EmbeddingError as error:
                logger.warning("memory_store_embedding_failed", error=str(error))
                continue
            await self.store.store(
                item, embedding=vectors[0], embedding_model=self.embedding_model
            )
            stored += 1
        if stored:
            logger.info("memories_stored", count=stored)
        return ExtractionOutcome(stored, reply.prompt_tokens, reply.completion_tokens)

    async def forget(
        self, *, subject_identity_id: str, conversation_stable_key: str | None
    ) -> int:
        return await self.store.revoke_for(
            subject_identity_id=subject_identity_id,
            conversation_stable_key=conversation_stable_key,
            now=self.now(),
        )

    def _to_item(self, candidate: "_Candidate", envelope: MessageEnvelope) -> MemoryItem:
        # Extraction can never produce SENSITIVE items; that privacy level is
        # reserved for explicit operator/user action in a later milestone.
        if candidate.scope == "subject":
            return MemoryItem(
                scope=MemoryScope.SUBJECT,
                subject_identity_id=envelope.sender_identity_id,
                kind=candidate.kind,
                content=candidate.content,
                source_message_ids=(envelope.id,),
                confidence=candidate.confidence,
                privacy=MemoryPrivacy.PRIVATE,
            )
        return MemoryItem(
            scope=MemoryScope.CONVERSATION,
            conversation=_conversation_key(envelope),
            kind=candidate.kind,
            content=candidate.content,
            source_message_ids=(envelope.id,),
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
    from mybot.engine.prompt import envelope_text

    return envelope_text(envelope) or "[non-text message]"


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
