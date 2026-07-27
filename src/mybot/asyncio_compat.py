"""Asyncio entrypoint helpers for Windows-compatible async database access."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def needs_selector_event_loop(*, platform: str | None = None) -> bool:
    """Report whether psycopg needs an override for the platform default loop."""
    current_platform = sys.platform if platform is None else platform
    return current_platform == "win32"


def new_selector_event_loop() -> asyncio.AbstractEventLoop:
    """Create the loop used by Windows process entrypoints."""
    return asyncio.SelectorEventLoop()


def uvicorn_loop(*, platform: str | None = None) -> str:
    """Select Uvicorn's importable selector factory only where it is required."""
    if needs_selector_event_loop(platform=platform):
        return "mybot.asyncio_compat:new_selector_event_loop"
    return "auto"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a top-level coroutine with the platform-compatible loop policy."""
    if not needs_selector_event_loop():
        return asyncio.run(coroutine)
    with asyncio.Runner(loop_factory=new_selector_event_loop) as runner:
        return runner.run(coroutine)
