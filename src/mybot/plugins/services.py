"""The deliberately small, versioned service surface exposed to plugins."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from mybot.infrastructure.llm import ChatMessage, LlmReply
from mybot.infrastructure.model_routing import EmbeddingBatch
from mybot.plugins.broker import PluginService
from mybot.repositories.memory import ScoredMemory


class KvStore(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...

    async def set(self, key: str, value: JsonValue) -> None: ...


class LlmCompleter(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


class Embeddings(Protocol):
    async def embed_with_model(self, texts: Sequence[str]) -> EmbeddingBatch: ...


class MemorySearch(Protocol):
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


@dataclass(slots=True)
class KvStoreEndpoint:
    store: KvStore

    async def __call__(
        self, plugin_id: str, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        action = arguments.get("action", "get")
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("kv.store requires a non-empty key up to 200 characters")
        namespaced = f"plugin.kv.{plugin_id}.{key}"
        if action == "get":
            return {"key": key, "value": await self.store.get(namespaced)}
        if action != "set" or "value" not in arguments:
            raise ValueError("kv.store action must be get or set")
        await self.store.set(namespaced, arguments["value"])
        return {"key": key, "stored": True}


@dataclass(slots=True)
class LlmCompleteEndpoint:
    llm: LlmCompleter

    async def __call__(
        self, plugin_id: str, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        del plugin_id
        prompt = arguments.get("prompt")
        system = arguments.get("system")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("llm.complete requires prompt text up to 20000 characters")
        messages: list[ChatMessage] = []
        if isinstance(system, str) and system.strip():
            messages.append(ChatMessage(role="system", content=system[:10_000]))
        messages.append(ChatMessage(role="user", content=prompt))
        reply = await self.llm.complete(messages)
        return {
            "text": reply.text,
            "model": reply.model,
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
        }


@dataclass(slots=True)
class MemorySearchEndpoint:
    memory: MemorySearch
    embeddings: Embeddings
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC)

    async def __call__(
        self, plugin_id: str, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        del plugin_id
        query = arguments.get("query")
        subject = arguments.get("subject_identity_id")
        conversation = arguments.get("conversation_stable_key")
        raw_limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 4_000:
            raise ValueError("memory.search requires query text")
        if not isinstance(subject, str) or not subject:
            raise ValueError("memory.search requires subject_identity_id")
        if not isinstance(conversation, str) or not conversation:
            raise ValueError("memory.search requires conversation_stable_key")
        limit = min(10, max(1, raw_limit if isinstance(raw_limit, int) else 5))
        batch = await self.embeddings.embed_with_model([query])
        rows = await self.memory.search(
            query_embedding=batch.vectors[0],
            query_text=query,
            embedding_model=batch.model,
            subject_identity_id=subject,
            conversation_stable_key=conversation,
            include_private=arguments.get("include_private") is True,
            now=self.now(),
            limit=limit,
        )
        return {
            "items": [
                {
                    "id": str(row.id),
                    "scope": row.scope.value,
                    "kind": row.kind,
                    "content": row.content,
                    "confidence": row.confidence,
                    "distance": row.distance,
                }
                for row in rows
            ]
        }


def plugin_services(
    *,
    kv: KvStore,
    llm: LlmCompleter,
    memory: MemorySearch,
    embeddings: Embeddings,
) -> tuple[PluginService, ...]:
    return (
        PluginService("memory.search", 1, "memory.read", MemorySearchEndpoint(memory, embeddings)),
        PluginService("llm.complete", 1, "llm.complete", LlmCompleteEndpoint(llm)),
        PluginService("kv.store", 1, "kv.store", KvStoreEndpoint(kv)),
    )


__all__ = ["plugin_services"]
