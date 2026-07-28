import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest

from mybot.repositories.memory import LifecycleReport
from mybot.runtime import ProcessMode
from mybot.services.maintenance import MaintenanceWorkerService, PluginTaskScheduler

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


@dataclass
class FakeMemoryMaintenance:
    fail_first: bool = False
    always_fail: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)

    async def run_lifecycle(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self.always_fail or (self.fail_first and len(self.calls) == 1):
            raise RuntimeError("database hiccup")
        return LifecycleReport(expired=1, decayed=2, deleted=3)


@dataclass
class FakeProactiveJob:
    always_fail: bool = False
    passes: int = 0

    async def run_pass(self) -> int:
        self.passes += 1
        if self.always_fail:
            raise RuntimeError("redis hiccup")
        return 0


def make_service(
    memory: FakeMemoryMaintenance,
    proactive: FakeProactiveJob | None = None,
) -> MaintenanceWorkerService:
    return MaintenanceWorkerService(
        memory=memory,
        interval_seconds=0.02,
        decay_days=90,
        decay_factor=0.8,
        confidence_floor=0.2,
        revoked_retention_days=30,
        proactive=proactive,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_runs_immediately_then_on_interval_and_stops_promptly() -> None:
    memory = FakeMemoryMaintenance()
    service = make_service(memory)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.MAINTENANCE_WORKER, stop_event))
    for _ in range(300):
        if len(memory.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(memory.calls) >= 3
    first = memory.calls[0]
    assert first["now"] == NOW
    assert first["decay_days"] == 90
    assert first["revoked_retention_days"] == 30


@pytest.mark.asyncio
async def test_one_failing_pass_does_not_kill_the_loop() -> None:
    memory = FakeMemoryMaintenance(fail_first=True)
    service = make_service(memory)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.MAINTENANCE_WORKER, stop_event))
    for _ in range(300):
        if len(memory.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(memory.calls) >= 2


@pytest.mark.asyncio
async def test_failing_memory_pass_never_blocks_the_proactive_pass() -> None:
    memory = FakeMemoryMaintenance(always_fail=True)
    proactive = FakeProactiveJob()
    service = make_service(memory, proactive)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.MAINTENANCE_WORKER, stop_event))
    for _ in range(300):
        if proactive.passes >= 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert proactive.passes >= 2
    assert len(memory.calls) >= 2


@pytest.mark.asyncio
async def test_failing_proactive_pass_never_blocks_the_memory_pass() -> None:
    memory = FakeMemoryMaintenance()
    proactive = FakeProactiveJob(always_fail=True)
    service = make_service(memory, proactive)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.MAINTENANCE_WORKER, stop_event))
    for _ in range(300):
        if len(memory.calls) >= 2 and proactive.passes >= 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(memory.calls) >= 2
    assert proactive.passes >= 2


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    service = make_service(FakeMemoryMaintenance())

    task = asyncio.create_task(service.run(ProcessMode.MAINTENANCE_WORKER, asyncio.Event()))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_plugin_task_scheduler_dispatches_due_tasks_and_obeys_intervals() -> None:
    now = [100.0]
    dispatched: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks"):
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {
                            "plugin_id": "example.jobs",
                            "task_id": "refresh",
                            "interval_seconds": 60,
                        }
                    ]
                },
            )
        body = __import__("json").loads(request.content)
        dispatched.append((body["plugin_id"], body["task_id"]))
        return httpx.Response(200, json={"delivered": 1})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api"
    ) as client:
        scheduler = PluginTaskScheduler(
            client=client,
            broker_url="http://api",
            clock=lambda: now[0],
        )
        assert await scheduler.run_pass() == 1
        assert await scheduler.run_pass() == 0
        now[0] += 60
        assert await scheduler.run_pass() == 1

    assert dispatched == [
        ("example.jobs", "refresh"),
        ("example.jobs", "refresh"),
    ]
