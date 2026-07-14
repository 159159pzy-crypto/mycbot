import asyncio
from dataclasses import dataclass, field

import pytest

from mybot.runtime import ProcessMode, run_mode


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
