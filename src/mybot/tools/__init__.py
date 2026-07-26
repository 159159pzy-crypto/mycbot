"""Policy-aware tool registry, execution, and citation extraction."""

import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

import structlog
from pydantic import JsonValue

from mybot.contracts import (
    Citation,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from mybot.contracts.json import thaw_json_object

logger = structlog.get_logger("mybot.tools")


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult: ...


class ToolCatalog(Protocol):
    """What the engine and executor need from any tool source."""

    def get(self, tool_id: str) -> Tool | None: ...

    def available(self, granted: Collection[str]) -> list[Tool]: ...

    def openai_tools(self, granted: Collection[str]) -> list[dict[str, object]]: ...

    async def refresh(self) -> None: ...


class ToolRegistry:
    """Holds tools and exposes only capability-satisfied ones to the model."""

    def __init__(self, tools: Collection[Tool]) -> None:
        self._tools: dict[str, Tool] = {tool.spec.id: tool for tool in tools}

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def available(self, granted: Collection[str]) -> list[Tool]:
        granted_set = set(granted)
        return [
            tool
            for tool in self._tools.values()
            if set(tool.spec.capabilities) <= granted_set
        ]

    def openai_tools(self, granted: Collection[str]) -> list[dict[str, object]]:
        return [to_openai_tool(tool.spec) for tool in self.available(granted)]

    async def refresh(self) -> None:
        """Static registries have nothing to refresh."""
        return None


class ApprovalSource(Protocol):
    async def is_approved(self, tool_id: str) -> bool: ...


@dataclass(slots=True)
class ToolExecutor:
    """Enforces capability/approval policy and a per-tool timeout on execution."""

    registry: ToolCatalog
    timeout_seconds: float = 15.0
    approvals: ApprovalSource | None = None

    async def execute(
        self,
        tool_id: str,
        context: ToolContext,
        arguments: Mapping[str, JsonValue],
    ) -> ToolResult:
        tool = self.registry.get(tool_id)
        if tool is None:
            return _fail("unknown_tool", f"no tool named {tool_id}")
        missing = set(tool.spec.capabilities) - set(context.granted_capabilities)
        if missing:
            return _fail(
                "capability_denied",
                f"missing required capabilities: {', '.join(sorted(missing))}",
            )
        if tool.spec.approval_required and not await self._approved(tool_id):
            return _fail(
                "approval_required",
                "this tool requires operator approval; grant it in the operator console",
            )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await tool.run(context, arguments)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("tool_timed_out", tool_id=tool_id)
            return _fail(
                "timeout",
                f"{tool_id} exceeded {self.timeout_seconds:g}s",
                retryable=True,
            )
        except Exception as error:
            logger.exception("tool_execution_failed", tool_id=tool_id)
            return _fail("tool_error", type(error).__name__)


    async def _approved(self, tool_id: str) -> bool:
        if self.approvals is None:
            return False
        try:
            return await self.approvals.is_approved(tool_id)
        except Exception:
            logger.exception("approval_lookup_failed", tool_id=tool_id)
            return False


def collect_citations(result: ToolResult) -> list[Citation]:
    """Read a `sources` list of {label, uri} from a successful result, deduped by uri."""

    if not result.ok or result.data is None:
        return []
    sources = result.data.get("sources")
    if not isinstance(sources, tuple):
        return []
    citations: list[Citation] = []
    seen: set[str] = set()
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        uri = entry.get("uri")
        label = entry.get("label")
        if not isinstance(uri, str) or not uri.strip() or uri in seen:
            continue
        seen.add(uri)
        citations.append(
            Citation(label=label if isinstance(label, str) and label.strip() else uri, uri=uri)
        )
    return citations


def to_openai_tool(spec: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": spec.id,
            "description": spec.description,
            "parameters": thaw_json_object(spec.input_schema),
        },
    }


def _fail(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message, retryable=retryable))


__all__ = [
    "Tool",
    "ToolCatalog",
    "ToolExecutor",
    "ToolRegistry",
    "collect_citations",
    "to_openai_tool",
]
