"""Cancellable lifecycle shared by non-API process roles."""

import asyncio
import signal
from enum import StrEnum
from types import FrameType
from typing import Protocol

import structlog


class ProcessMode(StrEnum):
    API = "api"
    GATEWAY = "gateway"
    AGENT_WORKER = "agent-worker"
    MAINTENANCE_WORKER = "maintenance-worker"
    PLUGIN_RUNNER = "plugin-runner"


class LifecycleService(Protocol):
    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None: ...


class IdleLifecycleService:
    """Foundation lifecycle that can later host concrete queues and adapters."""

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger = structlog.get_logger("mybot.runtime").bind(process_mode=mode.value)
        logger.info("process_started")
        try:
            await stop_event.wait()
        finally:
            logger.info("process_stopped")


async def run_mode(
    mode: ProcessMode,
    *,
    service: LifecycleService | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    if mode is ProcessMode.API:
        raise ValueError("API mode is served by uvicorn")
    resolved_event = stop_event or asyncio.Event()
    await (service or IdleLifecycleService()).run(mode, resolved_event)


async def run_process(mode: ProcessMode, *, service: LifecycleService | None = None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop_event.set()

    def handle_signal(_signal_number: int, _frame: FrameType | None) -> None:
        request_stop()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, request_stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(signal_name, handle_signal)

    await run_mode(mode, service=service, stop_event=stop_event)
