import asyncio
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest
from crash_plugin_fixture import PLUGIN as CRASH_PLUGIN
from pydantic import JsonValue

from mybot.api import create_app
from mybot.contracts import PluginManifest, ToolRisk, ToolSpec
from mybot.infrastructure.health import ReadinessService
from mybot.plugins.examples import dice
from mybot.plugins.runner import PluginRunnerService, load_plugins
from mybot.plugins.sdk import SimplePlugin
from mybot.runtime import ProcessMode
from mybot.settings import Settings


class AlwaysUpProbe:
    async def check(self) -> None:
        return None


def slow_plugin(delay_seconds: float) -> SimplePlugin:
    async def _slow(arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        await asyncio.sleep(delay_seconds)
        return {"done": True}

    return SimplePlugin(
        manifest=PluginManifest(
            id="test.slow",
            version="0.1.0",
            entrypoint="does.not.matter:PLUGIN",
            tools=(
                ToolSpec.model_validate(
                    {
                        "id": "slow_tool",
                        "description": "sleeps",
                        "input_schema": {"type": "object", "properties": {}},
                        "read_only": True,
                        "idempotent": True,
                        "risk": ToolRisk.NONE,
                        "capabilities": (),
                        "approval_required": False,
                    }
                ),
            ),
        ),
        tool_handlers={"slow_tool": _slow},
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings=Settings(plugin_invoke_timeout_seconds=3.0),
        readiness=ReadinessService(database=AlwaysUpProbe(), redis=AlwaysUpProbe()),
        bootstrap=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as instance:
        yield instance


def make_runner(
    client: httpx.AsyncClient, plugins: list[SimplePlugin], *, call_timeout: float = 2.0
) -> PluginRunnerService:
    return PluginRunnerService(
        broker_url="http://api",
        plugins=plugins,
        client=client,
        poll_wait_seconds=0.1,
        call_timeout_seconds=call_timeout,
        reconnect_initial_seconds=0.01,
        reconnect_max_seconds=0.05,
    )


async def wait_until(predicate, timeout_seconds: float = 5.0):  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before the timeout")


@pytest.mark.asyncio
async def test_dice_plugin_registers_and_answers_end_to_end(
    client: httpx.AsyncClient,
) -> None:
    runner = make_runner(client, [dice.PLUGIN])
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(ProcessMode.PLUGIN_RUNNER, stop_event))
    try:

        async def registered() -> bool:
            return bool((await client.get("/plugin-broker/tools")).json()["tools"])

        await wait_until(registered)
        response = await client.post(
            "/plugin-broker/invoke",
            json={"tool_id": "roll_dice", "arguments": {"sides": 6, "count": 2}},
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["ok"] is True
        rolls = result["data"]["rolls"]
        assert len(rolls) == 2
        assert all(1 <= roll <= 6 for roll in rolls)
        assert result["data"]["total"] == sum(rolls)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_crashing_plugin_fails_structured_and_loop_keeps_serving(
    client: httpx.AsyncClient,
) -> None:
    runner = make_runner(client, [dice.PLUGIN, CRASH_PLUGIN])
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(ProcessMode.PLUGIN_RUNNER, stop_event))
    try:

        async def registered() -> bool:
            tools = (await client.get("/plugin-broker/tools")).json()["tools"]
            return len(tools) == 2

        await wait_until(registered)
        crash = await client.post(
            "/plugin-broker/invoke", json={"tool_id": "crash_now", "arguments": {}}
        )
        assert crash.json()["result"]["ok"] is False
        assert crash.json()["result"]["error"]["code"] == "tool_error"

        healthy = await client.post(
            "/plugin-broker/invoke", json={"tool_id": "roll_dice", "arguments": {}}
        )
        assert healthy.json()["result"]["ok"] is True
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_slow_tool_times_out_with_structured_failure(
    client: httpx.AsyncClient,
) -> None:
    runner = make_runner(client, [slow_plugin(1.0)], call_timeout=0.05)
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(ProcessMode.PLUGIN_RUNNER, stop_event))
    try:

        async def registered() -> bool:
            return bool((await client.get("/plugin-broker/tools")).json()["tools"])

        await wait_until(registered)
        response = await client.post(
            "/plugin-broker/invoke", json={"tool_id": "slow_tool", "arguments": {}}
        )

        assert response.json()["result"]["error"]["code"] == "timeout"
        assert response.json()["result"]["error"]["retryable"] is True
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_events_reach_hooked_plugins(client: httpx.AsyncClient) -> None:
    runner = make_runner(client, [dice.PLUGIN])
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run(ProcessMode.PLUGIN_RUNNER, stop_event))
    try:

        async def registered() -> bool:
            return bool((await client.get("/plugin-broker/tools")).json()["tools"])

        await wait_until(registered)
        before = dice.seen_events()
        await client.post(
            "/plugin-broker/events",
            json={"kind": "message", "payload": {"envelope_id": "qq:qq-main:1"}},
        )

        async def delivered() -> bool:
            return dice.seen_events() > before

        await wait_until(delivered)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)


def test_load_plugins_skips_broken_entries_and_keeps_the_rest() -> None:
    config = (
        '{"plugins": ["mybot.plugins.examples.dice:PLUGIN", '
        '"no.such.module:PLUGIN", "mybot.plugins.examples.dice:not_there", '
        '"mybot.plugins.examples.dice:seen_events", 42]}'
    )

    plugins = load_plugins(config)

    assert [plugin.manifest.id for plugin in plugins] == ["example.dice"]
    assert load_plugins("not json") == []
    assert load_plugins('{"plugins": "not-a-list"}') == []
