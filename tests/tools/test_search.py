from dataclasses import dataclass, field
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from mybot.contracts import ChatKind, ConversationKey, ToolContext
from mybot.tools.search import SearxngSearchTool


@dataclass
class FakeSearxng:
    results: list[dict[str, str]]
    status_code: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="upstream error")
        return httpx.Response(200, json={"results": self.results})


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=("web.search",),
        correlation_id="corr-1",
    )


def make_tool(api: FakeSearxng, *, max_results: int = 5) -> SearxngSearchTool:
    return SearxngSearchTool(
        client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        searxng_url="http://searxng.example:8080",
        max_results=max_results,
    )


def test_spec_declares_search_capability_and_no_approval() -> None:
    tool = make_tool(FakeSearxng(results=[]))

    assert tool.spec.id == "web_search"
    assert tool.spec.capabilities == ("web.search",)
    assert tool.spec.approval_required is False
    assert tool.spec.read_only is True


@pytest.mark.asyncio
async def test_search_normalizes_results_and_emits_sources() -> None:
    api = FakeSearxng(
        results=[
            {"title": "Weather today", "url": "https://a.example/1", "content": "sunny"},
            {"title": "Forecast", "url": "https://b.example/2", "content": "rain"},
        ]
    )
    tool = make_tool(api)

    result = await tool.run(context(), {"query": "weather"})

    assert result.ok is True
    assert result.data is not None
    results = result.data["results"]
    assert isinstance(results, tuple)
    assert results[0]["title"] == "Weather today"
    assert results[0]["url"] == "https://a.example/1"
    assert results[0]["snippet"] == "sunny"
    sources = result.data["sources"]
    assert dict(sources[0]) == {"label": "Weather today", "uri": "https://a.example/1"}
    query = parse_qs(api.requests[0].url.query.decode())
    assert query["q"] == ["weather"]
    assert query["format"] == ["json"]


@pytest.mark.asyncio
async def test_search_applies_top_n_cap() -> None:
    api = FakeSearxng(
        results=[
            {"title": f"r{i}", "url": f"https://x.example/{i}", "content": "c"}
            for i in range(10)
        ]
    )
    tool = make_tool(api, max_results=3)

    result = await tool.run(context(), {"query": "many"})

    assert result.data is not None
    assert len(result.data["results"]) == 3


@pytest.mark.asyncio
async def test_missing_query_argument_is_a_structured_failure() -> None:
    tool = make_tool(FakeSearxng(results=[]))

    result = await tool.run(context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"


@pytest.mark.asyncio
async def test_upstream_error_is_structured_failure() -> None:
    tool = make_tool(FakeSearxng(results=[], status_code=502))

    result = await tool.run(context(), {"query": "weather"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "search_failed"
    assert result.error.retryable is True
