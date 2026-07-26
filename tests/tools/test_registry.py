import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from mybot.contracts import (
    ChatKind,
    Citation,
    ConversationKey,
    ToolContext,
    ToolError,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from mybot.contracts.json import JsonValue
from mybot.tools import ToolExecutor, ToolRegistry, collect_citations


def spec(
    tool_id: str,
    *,
    capabilities: tuple[str, ...] = (),
    approval_required: bool = False,
) -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "id": tool_id,
            "description": f"the {tool_id} tool",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "read_only": True,
            "idempotent": True,
            "risk": ToolRisk.LOW,
            "capabilities": capabilities,
            "approval_required": approval_required,
        }
    )


@dataclass
class FakeTool:
    spec: ToolSpec
    result: ToolResult | None = None
    error: Exception | None = None
    delay_seconds: float = 0.0
    calls: list[dict[str, JsonValue]] = field(default_factory=list)

    async def run(
        self, context: ToolContext, arguments: dict[str, JsonValue]
    ) -> ToolResult:
        self.calls.append(dict(arguments))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def context(*, granted: tuple[str, ...] = ()) -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=granted,
        correlation_id="corr-1",
    )


def ok_tool(tool_id: str = "web_search", **kwargs: object) -> FakeTool:
    return FakeTool(
        spec=spec(tool_id, **kwargs),  # type: ignore[arg-type]
        result=ToolResult.success({"answer": "42"}),
    )


def test_registry_converts_specs_to_openai_function_tools() -> None:
    registry = ToolRegistry([ok_tool("web_search", capabilities=("web.search",))])

    tools = registry.openai_tools(granted=("web.search",))

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "the web_search tool",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


def test_registry_lists_only_capability_satisfied_tools() -> None:
    registry = ToolRegistry(
        [
            ok_tool("web_search", capabilities=("web.search",)),
            ok_tool("db_admin", capabilities=("db.admin",)),
        ]
    )

    names = {tool["function"]["name"] for tool in registry.openai_tools(granted=("web.search",))}

    assert names == {"web_search"}


@pytest.mark.asyncio
async def test_execute_returns_tool_result_on_success() -> None:
    tool = ok_tool("web_search", capabilities=("web.search",))
    executor = ToolExecutor(ToolRegistry([tool]))

    result = await executor.execute(
        "web_search", context(granted=("web.search",)), {"query": "hi"}
    )

    assert result.ok is True
    assert tool.calls == [{"query": "hi"}]


@pytest.mark.asyncio
async def test_missing_capability_is_denied_without_running() -> None:
    tool = ok_tool("web_search", capabilities=("web.search",))
    executor = ToolExecutor(ToolRegistry([tool]))

    result = await executor.execute("web_search", context(granted=()), {"query": "hi"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "capability_denied"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_approval_required_tool_is_blocked_with_operator_message() -> None:
    tool = ok_tool("dangerous", approval_required=True)
    executor = ToolExecutor(ToolRegistry([tool]))

    result = await executor.execute("dangerous", context(), {"query": "hi"})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "approval_required"
    assert tool.calls == []


@pytest.mark.asyncio
async def test_unknown_tool_is_a_structured_failure() -> None:
    executor = ToolExecutor(ToolRegistry([]))

    result = await executor.execute("ghost", context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unknown_tool"


@pytest.mark.asyncio
async def test_timeout_yields_retryable_failure() -> None:
    tool = FakeTool(spec=spec("slow"), delay_seconds=1.0, result=ToolResult.success({"x": 1}))
    executor = ToolExecutor(ToolRegistry([tool]), timeout_seconds=0.02)

    result = await executor.execute("slow", context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True


@pytest.mark.asyncio
async def test_unexpected_exception_becomes_tool_error() -> None:
    tool = FakeTool(spec=spec("boom"), error=RuntimeError("kaboom"))
    executor = ToolExecutor(ToolRegistry([tool]))

    result = await executor.execute("boom", context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "tool_error"


@pytest.mark.asyncio
async def test_cancellation_propagates_through_the_executor() -> None:
    tool = FakeTool(spec=spec("slow"), delay_seconds=1.0, result=ToolResult.success({"x": 1}))
    executor = ToolExecutor(ToolRegistry([tool]), timeout_seconds=5.0)

    task = asyncio.create_task(executor.execute("slow", context(), {}))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_collect_citations_reads_sources_from_result_data() -> None:
    result = ToolResult.success(
        {
            "answer": "...",
            "sources": [
                {"label": "Example", "uri": "https://example.com/a"},
                {"label": "Example", "uri": "https://example.com/a"},  # duplicate uri
                {"label": "Other", "uri": "https://example.org/b"},
            ],
        }
    )

    citations = collect_citations(result)

    assert citations == [
        Citation(label="Example", uri="https://example.com/a"),
        Citation(label="Other", uri="https://example.org/b"),
    ]


def test_collect_citations_is_empty_for_failures_and_missing_sources() -> None:
    failure = ToolResult.failure(ToolError(code="x", message="y"))
    no_sources = ToolResult.success({"answer": "no sources here"})

    assert collect_citations(failure) == []
    assert collect_citations(no_sources) == []
