import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue

from mybot.api import create_app
from mybot.contracts import (
    ChatKind,
    ConversationKey,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from mybot.infrastructure.health import ReadinessService
from mybot.plugins.examples import dice
from mybot.plugins.runner import PluginRunnerService
from mybot.runtime import ProcessMode
from mybot.settings import Settings
from mybot.tools import ToolExecutor, ToolRegistry
from mybot.tools.plugins import BrokerToolCatalog, PluginToolProxy


class AlwaysUpProbe:
    async def check(self) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def local_tool_spec(tool_id: str = "web_search") -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "id": tool_id,
            "description": "builtin",
            "input_schema": {"type": "object", "properties": {}},
            "read_only": True,
            "idempotent": True,
            "risk": ToolRisk.LOW,
            "capabilities": [],
            "approval_required": False,
        }
    )


@dataclass
class LocalTool:
    spec_value: ToolSpec = field(default_factory=local_tool_spec)

    @property
    def spec(self) -> ToolSpec:
        return self.spec_value

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        return ToolResult.success({"builtin": True})


def context() -> ToolContext:
    return ToolContext(
        invocation_id=uuid4(),
        conversation=ConversationKey(
            connection_id="telegram-main", chat_kind=ChatKind.DIRECT, chat_id="777"
        ),
        actor_identity_id="telegram:777",
        granted_capabilities=(),
        correlation_id="corr-1",
    )


def broker_tools_response(tool_id: str = "roll_dice") -> dict[str, object]:
    return {
        "tools": [
            {
                "plugin_id": "example.dice",
                "spec": {
                    "id": tool_id,
                    "description": "roll",
                    "input_schema": {"type": "object", "properties": {}},
                    "read_only": True,
                    "idempotent": False,
                    "risk": "NONE",
                    "capabilities": [],
                    "approval_required": False,
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_catalog_merges_broker_tools_and_respects_ttl() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=broker_tools_response())

    clock = Clock()
    catalog = BrokerToolCatalog(
        static=ToolRegistry([LocalTool()]),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        broker_url="http://api",
        ttl_seconds=30.0,
        clock=clock,
    )

    await catalog.refresh()
    await catalog.refresh()  # inside TTL: no second fetch
    assert calls["count"] == 1
    clock.now += 31.0
    await catalog.refresh()
    assert calls["count"] == 2

    names = {tool["function"]["name"] for tool in catalog.openai_tools(granted=())}
    assert names == {"web_search", "roll_dice"}
    assert catalog.get("web_search") is not None
    assert isinstance(catalog.get("roll_dice"), PluginToolProxy)


@pytest.mark.asyncio
async def test_plugin_tool_shadowing_a_builtin_is_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broker_tools_response(tool_id="web_search"))

    static = ToolRegistry([LocalTool()])
    catalog = BrokerToolCatalog(
        static=static,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        broker_url="http://api",
    )

    await catalog.refresh()

    assert catalog.get("web_search") is static.get("web_search")
    assert len(catalog.openai_tools(granted=())) == 1


@pytest.mark.asyncio
async def test_broker_outage_degrades_to_static_tools_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    catalog = BrokerToolCatalog(
        static=ToolRegistry([LocalTool()]),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        broker_url="http://api",
    )

    await catalog.refresh()

    names = {tool["function"]["name"] for tool in catalog.openai_tools(granted=())}
    assert names == {"web_search"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(404, "unknown_tool"), (504, "timeout"), (500, "plugin_error")],
)
async def test_proxy_maps_broker_failures_to_structured_results(
    status: int, expected_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    proxy = PluginToolProxy(
        _spec=local_tool_spec("roll_dice"),
        plugin_id="example.dice",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        broker_url="http://api",
    )

    result = await proxy.run(context(), {})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == expected_code


@pytest.mark.asyncio
async def test_executor_to_runner_dice_acceptance_end_to_end() -> None:
    app = create_app(
        settings=Settings(plugin_invoke_timeout_seconds=3.0),
        readiness=ReadinessService(database=AlwaysUpProbe(), redis=AlwaysUpProbe()),
        bootstrap=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        runner = PluginRunnerService(
            broker_url="http://api",
            plugins=[dice.PLUGIN],
            client=client,
            poll_wait_seconds=0.1,
            call_timeout_seconds=2.0,
            reconnect_initial_seconds=0.01,
        )
        stop_event = asyncio.Event()
        runner_task = asyncio.create_task(
            runner.run(ProcessMode.PLUGIN_RUNNER, stop_event)
        )
        try:
            catalog = BrokerToolCatalog(
                static=ToolRegistry([]),
                client=client,
                broker_url="http://api",
                ttl_seconds=0.01,
            )
            executor = ToolExecutor(catalog, timeout_seconds=5.0)
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                await catalog.refresh()
                if catalog.get("roll_dice") is not None:
                    break
                await asyncio.sleep(0.05)

            result = await executor.execute(
                "roll_dice", context(), {"sides": 20, "count": 1}
            )

            assert result.ok is True
            assert result.data is not None
            rolls = result.data["rolls"]
            assert isinstance(rolls, tuple) and len(rolls) == 1
            roll = rolls[0]
            assert isinstance(roll, int) and 1 <= roll <= 20
        finally:
            stop_event.set()
            await asyncio.wait_for(runner_task, timeout=3.0)
