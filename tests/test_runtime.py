import asyncio
from dataclasses import dataclass, field

import pytest

from mybot.runtime import ProcessMode, run_mode, run_process
from mybot.services import create_service
from mybot.settings import Settings


@dataclass
class RecordingLifecycle:
    modes: list[ProcessMode] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        self.modes.append(mode)
        self.started.set()
        await stop_event.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        ProcessMode.GATEWAY,
        ProcessMode.AGENT_WORKER,
        ProcessMode.MAINTENANCE_WORKER,
        ProcessMode.PLUGIN_RUNNER,
    ],
)
async def test_non_api_modes_run_through_cancellable_lifecycle(mode: ProcessMode) -> None:
    lifecycle = RecordingLifecycle()
    stop_event = asyncio.Event()

    running = asyncio.create_task(run_mode(mode, service=lifecycle, stop_event=stop_event))
    await asyncio.wait_for(lifecycle.started.wait(), timeout=0.5)

    assert running.done() is False
    stop_event.set()
    await asyncio.wait_for(running, timeout=0.5)

    assert lifecycle.modes == [mode]


@pytest.mark.asyncio
async def test_run_process_passes_the_injected_service_through() -> None:
    lifecycle = RecordingLifecycle()

    running = asyncio.create_task(
        run_process(ProcessMode.MAINTENANCE_WORKER, service=lifecycle)
    )
    await asyncio.wait_for(lifecycle.started.wait(), timeout=0.5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert lifecycle.modes == [ProcessMode.MAINTENANCE_WORKER]


def test_worker_gateway_and_maintenance_modes_get_real_services() -> None:
    from mybot.services.agent_worker import AgentWorkerService
    from mybot.services.gateway import GatewayService
    from mybot.services.maintenance import MaintenanceWorkerService

    settings = Settings()

    assert isinstance(create_service(ProcessMode.AGENT_WORKER, settings), AgentWorkerService)
    gateway = create_service(ProcessMode.GATEWAY, settings)
    assert isinstance(gateway, GatewayService)
    assert gateway.qq is None  # no NAPCAT_WS_URL configured in tests
    assert isinstance(
        create_service(ProcessMode.MAINTENANCE_WORKER, settings), MaintenanceWorkerService
    )
    assert create_service(ProcessMode.PLUGIN_RUNNER, settings) is None


def test_plugin_runner_mode_gets_a_real_service_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mybot.plugins.control import PluginSource
    from mybot.plugins.supervisor import PluginSupervisorService

    monkeypatch.setenv("MYBOT_PLUGIN_BROKER_URL", "http://api:8000")
    monkeypatch.setenv(
        "MYBOT_PLUGIN_CONFIG",
        '{"plugins": ["mybot.plugins.examples.dice:PLUGIN"]}',
    )
    service = create_service(ProcessMode.PLUGIN_RUNNER, Settings())

    assert isinstance(service, PluginSupervisorService)
    assert service.store.sources(service.config_json) == (
        PluginSource(entrypoint="mybot.plugins.examples.dice:PLUGIN"),
    )
