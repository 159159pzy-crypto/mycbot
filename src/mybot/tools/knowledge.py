"""Read-only knowledge-base retrieval tool."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from mybot.contracts import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec
from mybot.engine.knowledge import KnowledgeSearchService

KB_SEARCH_SPEC = ToolSpec.model_validate(
    {
        "id": "kb_search",
        "description": (
            "Search operator-provided documents. Matching uses child chunks and "
            "returns their parent context plus citation sources."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "read_only": True,
        "idempotent": True,
        "risk": ToolRisk.LOW,
        "capabilities": ("knowledge.read",),
        "approval_required": False,
    }
)


@dataclass(slots=True)
class KnowledgeSearchTool:
    service: KnowledgeSearchService
    sandbox_connection_id: str = "sandbox"

    @property
    def spec(self) -> ToolSpec:
        return KB_SEARCH_SPEC

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult.failure(
                ToolError(code="invalid_arguments", message="query is required")
            )
        raw_top_k = arguments.get("top_k", 5)
        raw_threshold = arguments.get("threshold", 0.35)
        if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
            return ToolResult.failure(
                ToolError(code="invalid_arguments", message="top_k must be an integer")
            )
        if not isinstance(raw_threshold, int | float) or isinstance(raw_threshold, bool):
            return ToolResult.failure(
                ToolError(code="invalid_arguments", message="threshold must be a number")
            )
        hits = await self.service.search(
            query.strip(),
            conversation_stable_key=context.conversation.stable_key,
            include_global=True,
            allow_conversation=context.conversation.connection_id != self.sandbox_connection_id,
            top_k=raw_top_k,
            threshold=float(raw_threshold),
        )
        results: list[JsonValue] = []
        sources: list[JsonValue] = []
        for hit in hits:
            uri = f"kb://{hit.document_id}/{hit.parent_id}"
            results.append(
                {
                    "document_id": str(hit.document_id),
                    "document_title": hit.document_title,
                    "child_id": str(hit.child_id),
                    "parent_id": str(hit.parent_id),
                    "matched_chunk": hit.child_content,
                    "context": hit.parent_content,
                    "score": hit.score,
                    "scope": hit.scope.value,
                    "uri": uri,
                }
            )
            sources.append({"label": hit.document_title, "uri": uri})
        return ToolResult.success(
            {"query": query.strip(), "results": results, "sources": sources}
        )


__all__ = ["KB_SEARCH_SPEC", "KnowledgeSearchTool"]
