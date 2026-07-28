from dataclasses import dataclass
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    ConversationKey,
    KnowledgeScope,
    KnowledgeSearchHit,
    ToolContext,
)
from mybot.tools import collect_citations
from mybot.tools.knowledge import KnowledgeSearchTool


@dataclass
class FakeSearch:
    kwargs: dict[str, object] | None = None

    async def search(self, query: str, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = {"query": query, **kwargs}
        return (
            KnowledgeSearchHit(
                child_id=uuid4(),
                parent_id=uuid4(),
                document_id=uuid4(),
                document_title="群规",
                child_content="禁止刷屏",
                parent_content="群规全文: 禁止刷屏, 友善交流。",
                score=0.91,
                scope=KnowledgeScope.GLOBAL,
            ),
        )


def context(connection_id: str = "qq-main") -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id=connection_id, chat_kind=ChatKind.DIRECT, chat_id="1"
        ),
        actor_identity_id="qq:1",
        granted_capabilities=("knowledge.read",),
        correlation_id="trace-1",
    )


@pytest.mark.asyncio
async def test_kb_search_returns_parent_context_and_citation() -> None:
    service = FakeSearch()
    result = await KnowledgeSearchTool(service=service).run(
        context(), {"query": "能刷屏吗", "top_k": 3, "threshold": 0.5}
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["results"][0]["context"] == "群规全文: 禁止刷屏, 友善交流。"
    assert collect_citations(result)[0].label == "群规"
    assert service.kwargs is not None
    assert service.kwargs["allow_conversation"] is True


@pytest.mark.asyncio
async def test_kb_search_sandbox_only_allows_global_scope() -> None:
    service = FakeSearch()
    await KnowledgeSearchTool(service=service).run(context("sandbox"), {"query": "规则"})
    assert service.kwargs is not None
    assert service.kwargs["allow_conversation"] is False
