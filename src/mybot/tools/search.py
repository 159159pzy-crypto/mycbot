"""SearXNG-backed web search tool."""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
import structlog
from pydantic import JsonValue

from mybot.adapters.payload import as_string_mapping, get_sequence, get_str
from mybot.contracts import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec

logger = structlog.get_logger("mybot.tools.search")

SEARCH_SPEC = ToolSpec.model_validate(
    {
        "id": "web_search",
        "description": (
            "Search the web for current information. Returns a list of results "
            "with titles, URLs, and short snippets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "read_only": True,
        "idempotent": True,
        "risk": ToolRisk.LOW,
        "capabilities": ("web.search",),
        "approval_required": False,
    }
)


@dataclass(slots=True)
class SearxngSearchTool:
    client: httpx.AsyncClient
    searxng_url: str
    max_results: int = 5

    @property
    def spec(self) -> ToolSpec:
        return SEARCH_SPEC

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail("invalid_arguments", "the 'query' argument is required")
        try:
            response = await self.client.get(
                f"{self.searxng_url.rstrip('/')}/search",
                params={"q": query, "format": "json"},
                timeout=self.client.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            logger.warning("searxng_request_failed", error=type(error).__name__)
            return _fail(
                "search_failed", f"search backend error: {type(error).__name__}", retryable=True
            )

        body = as_string_mapping(payload) or {}
        results: list[JsonValue] = []
        sources: list[JsonValue] = []
        for raw in get_sequence(body, "results")[: self.max_results]:
            entry = as_string_mapping(raw)
            if entry is None:
                continue
            title = get_str(entry, "title") or ""
            url = get_str(entry, "url") or ""
            snippet = get_str(entry, "content") or ""
            if not url:
                continue
            results.append({"title": title, "url": url, "snippet": snippet})
            sources.append({"label": title or url, "uri": url})
        return ToolResult.success({"query": query, "results": results, "sources": sources})


def _fail(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message, retryable=retryable))
