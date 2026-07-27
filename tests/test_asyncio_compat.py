import asyncio

from mybot.asyncio_compat import needs_selector_event_loop, run, uvicorn_loop


def test_non_windows_keeps_the_runtime_default_loop_factory() -> None:
    assert needs_selector_event_loop(platform="linux") is False
    assert uvicorn_loop(platform="linux") == "auto"


def test_windows_selects_the_psycopg_compatible_event_loop_factory() -> None:
    assert needs_selector_event_loop(platform="win32") is True
    assert uvicorn_loop(platform="win32") == "mybot.asyncio_compat:new_selector_event_loop"


def test_run_executes_a_top_level_coroutine() -> None:
    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    loop = run(current_loop())

    assert loop.is_closed()
