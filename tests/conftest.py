import asyncio
import sys

import pytest


class SelectorEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """Keep pytest-asyncio compatible with psycopg on Windows."""

    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop()


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return SelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()
