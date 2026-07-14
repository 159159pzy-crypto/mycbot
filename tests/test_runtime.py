import asyncio
from dataclasses import dataclass, field

import pytest

from mybot.runtime import ProcessMode, run_mode


@dataclass
class RecordingLifecycle:
    modes: list[ProcessMode] = field(default_factory=list)

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        self.modes.append(mode)
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
    stop_event.set()

    await run_mode(mode, service=lifecycle, stop_event=stop_event)

    assert lifecycle.modes == [mode]
