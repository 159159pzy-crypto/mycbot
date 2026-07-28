import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from mybot.plugins.control import PluginControlStore, PluginSource
from mybot.plugins.supervisor import PluginSupervisorService, SubprocessPluginInspector


@dataclass
class FakeProcess:
    pid: int
    returncode: int | None = None
    terminated: bool = False
    killed: bool = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@dataclass
class HangingProcess(FakeProcess):
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        super().kill()
        self.exit_event.set()

    async def wait(self) -> int:
        await self.exit_event.wait()
        assert self.returncode is not None
        return self.returncode


class FakeSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[PluginSource, str, dict[str, JsonValue]]] = []
        self.processes: list[FakeProcess] = []

    async def spawn(
        self,
        source: PluginSource,
        runner_id: str,
        config: dict[str, JsonValue],
    ) -> FakeProcess:
        self.calls.append((source, runner_id, config))
        process = FakeProcess(pid=100 + len(self.calls))
        self.processes.append(process)
        return process


class HangingFirstSpawner(FakeSpawner):
    async def spawn(
        self,
        source: PluginSource,
        runner_id: str,
        config: dict[str, JsonValue],
    ) -> FakeProcess:
        self.calls.append((source, runner_id, config))
        process: FakeProcess
        if not self.processes:
            process = HangingProcess(pid=100)
        else:
            process = FakeProcess(pid=100 + len(self.calls))
        self.processes.append(process)
        return process


class FakeInspector:
    async def inspect(self, source: PluginSource) -> str:
        return (
            "example.dice"
            if source.entrypoint == "mybot.plugins.examples.dice:PLUGIN"
            else "test.crasher"
        )


@pytest.mark.asyncio
async def test_subprocess_inspector_reads_manifest_outside_supervisor() -> None:
    plugin_id = await SubprocessPluginInspector().inspect(
        PluginSource("mybot.plugins.examples.dice:PLUGIN")
    )

    assert plugin_id == "example.dice"


@pytest.mark.asyncio
async def test_supervisor_reloads_only_target_plugin_and_persists_status(tmp_path: Path) -> None:
    store = PluginControlStore(tmp_path)
    spawner = FakeSpawner()
    unregistered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/unregister"):
            unregistered.append(__import__("json").loads(request.content)["runner_id"])
        return httpx.Response(200, json={"removed": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        supervisor = PluginSupervisorService(
            broker_url="http://api",
            config_json=(
                '{"plugins": ["mybot.plugins.examples.dice:PLUGIN", '
                '"crash_plugin_fixture:PLUGIN"]}'
            ),
            store=store,
            client=client,
            inspector=FakeInspector(),
            spawner=spawner,
            poll_seconds=0.01,
        )
        await supervisor.reconcile()
        assert len(spawner.calls) == 2
        first_processes = dict(supervisor.processes())

        store.request_action("example.dice", "reload")
        await supervisor.reconcile()

    second_processes = dict(supervisor.processes())
    assert first_processes["example.dice"] != second_processes["example.dice"]
    assert first_processes["test.crasher"] == second_processes["test.crasher"]
    assert any(item.startswith("plugin-") for item in unregistered)
    status = store.status()["plugins"]
    assert status["example.dice"]["state"] == "running"


@pytest.mark.asyncio
async def test_supervisor_kills_child_that_ignores_graceful_reload(tmp_path: Path) -> None:
    store = PluginControlStore(tmp_path)
    spawner = HangingFirstSpawner()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"removed": True})
        ),
        base_url="http://api",
    ) as client:
        supervisor = PluginSupervisorService(
            broker_url="http://api",
            config_json='{"plugins": ["mybot.plugins.examples.dice:PLUGIN"]}',
            store=store,
            client=client,
            inspector=FakeInspector(),
            spawner=spawner,
            stop_timeout_seconds=0.01,
            kill_timeout_seconds=0.1,
        )
        await supervisor.reconcile()
        first = spawner.processes[0]

        store.request_action("example.dice", "reload")
        await supervisor.reconcile()

    assert first.terminated is True
    assert first.killed is True
    assert len(spawner.processes) == 2
    assert dict(supervisor.processes())["example.dice"] != first.pid


@pytest.mark.asyncio
async def test_supervisor_uses_backoff_then_opens_circuit_until_manual_reload(
    tmp_path: Path,
) -> None:
    store = PluginControlStore(tmp_path)
    spawner = FakeSpawner()
    now = [10.0]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"removed": True})
        ),
        base_url="http://api",
    ) as client:
        supervisor = PluginSupervisorService(
            broker_url="http://api",
            config_json='{"plugins": ["crash_plugin_fixture:PLUGIN"]}',
            store=store,
            client=client,
            inspector=FakeInspector(),
            spawner=spawner,
            crash_limit=2,
            crash_window_seconds=60,
            clock=lambda: now[0],
        )
        await supervisor.reconcile()
        spawner.processes[-1].returncode = 1
        await supervisor.reconcile()
        assert store.status()["plugins"]["test.crasher"]["state"] == "backoff"

        now[0] += 1
        await supervisor.reconcile()
        spawner.processes[-1].returncode = 1
        await supervisor.reconcile()
        assert store.status()["plugins"]["test.crasher"]["state"] == "circuit_open"
        calls_at_circuit = len(spawner.calls)

        now[0] += 100
        await supervisor.reconcile()
        assert len(spawner.calls) == calls_at_circuit

        store.request_action("test.crasher", "reload")
        await supervisor.reconcile()
        assert len(spawner.calls) == calls_at_circuit + 1
