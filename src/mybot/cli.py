"""Command-line entrypoints for all deployable process roles."""

import argparse
from collections.abc import Sequence

import uvicorn

from mybot.asyncio_compat import run, uvicorn_loop
from mybot.infrastructure.logging import configure_logging
from mybot.runtime import ProcessMode, run_process
from mybot.settings import Settings


def _run_api(settings: Settings) -> None:
    uvicorn.run(
        "mybot.api:app",
        host=settings.api_host,
        port=settings.api_port,
        loop=uvicorn_loop(),
        log_config=None,
    )


def _run_non_api(mode: ProcessMode, settings: Settings) -> None:
    configure_logging(settings.log_level)
    from mybot.services import create_service

    run(run_process(mode, service=create_service(mode, settings)))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mybot")
    parser.add_argument("mode", choices=[mode.value for mode in ProcessMode])
    parsed = parser.parse_args(argv)
    mode = ProcessMode(parsed.mode)
    settings = Settings()
    if mode is ProcessMode.API:
        _run_api(settings)
    else:
        _run_non_api(mode, settings)


def api_main() -> None:
    main([ProcessMode.API.value])


def gateway_main() -> None:
    main([ProcessMode.GATEWAY.value])


def agent_worker_main() -> None:
    main([ProcessMode.AGENT_WORKER.value])


def maintenance_worker_main() -> None:
    main([ProcessMode.MAINTENANCE_WORKER.value])


def plugin_runner_main() -> None:
    main([ProcessMode.PLUGIN_RUNNER.value])
