"""Command-line entrypoints for all deployable process roles."""

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from mybot.asyncio_compat import run, uvicorn_loop
from mybot.infrastructure.logging import configure_logging
from mybot.infrastructure.telemetry import configure_process_telemetry
from mybot.runtime import ProcessMode, run_process
from mybot.settings import Settings


async def _snapshot_service(settings: Settings):  # type: ignore[no-untyped-def]
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.repositories.system_kv import SystemKvRepository
    from mybot.skills import SkillStore
    from mybot.snapshot import AgentSnapshotService, DatabaseSnapshotRepository

    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    service = AgentSnapshotService(
        repository=DatabaseSnapshotRepository(sessions),
        skills=SkillStore(Path(settings.skills_dir)),
        config=SystemKvRepository(sessions),
    )
    return service, engine


async def _export_snapshot(settings: Settings, output: Path, *, include_history: bool) -> None:
    from mybot.snapshot import write_snapshot

    service, engine = await _snapshot_service(settings)
    try:
        write_snapshot(output, await service.export(include_history=include_history))
    finally:
        await engine.dispose()


async def _import_snapshot(settings: Settings, source: Path) -> dict[str, int]:
    from mybot.snapshot import read_snapshot

    service, engine = await _snapshot_service(settings)
    try:
        return await service.import_snapshot(read_snapshot(source))
    finally:
        await engine.dispose()


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
    configure_process_telemetry(settings, service_name=f"mybot-{mode.value}")
    from mybot.services import create_service

    run(run_process(mode, service=create_service(mode, settings)))


def main(argv: Sequence[str] | None = None) -> None:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if argv and argv[0] == "plugin":
        plugin_parser = argparse.ArgumentParser(prog="mybot plugin")
        plugin_subcommands = plugin_parser.add_subparsers(dest="command", required=True)
        new_parser = plugin_subcommands.add_parser("new")
        new_parser.add_argument("name")
        new_parser.add_argument("--root", default="plugins")
        parsed_plugin = plugin_parser.parse_args(argv[1:])
        if parsed_plugin.command == "new":
            from mybot.plugins.tooling import scaffold_plugin

            created = scaffold_plugin(parsed_plugin.name, Path(parsed_plugin.root))
            print(created)
            return
    if argv and argv[0] == "eval":
        eval_parser = argparse.ArgumentParser(prog="mybot eval")
        eval_parser.add_argument("--offline", action="store_true", required=True)
        eval_parser.add_argument("--cases", default="evals")
        parsed_eval = eval_parser.parse_args(argv[1:])
        from mybot.evaluation import EvaluationCaseStore, run_offline_cases

        passed, failed = run_offline_cases(EvaluationCaseStore(Path(parsed_eval.cases)))
        print(f"offline evaluation: {passed} passed, {failed} failed")
        if failed:
            raise SystemExit(1)
        return
    if argv and argv[0] == "export":
        export_parser = argparse.ArgumentParser(prog="mybot export")
        export_parser.add_argument("--output", required=True, type=Path)
        export_parser.add_argument("--include-history", action="store_true")
        parsed_export = export_parser.parse_args(argv[1:])
        settings = Settings()
        run(
            _export_snapshot(
                settings,
                parsed_export.output,
                include_history=parsed_export.include_history,
            )
        )
        print(parsed_export.output)
        return
    if argv and argv[0] == "import":
        import_parser = argparse.ArgumentParser(prog="mybot import")
        import_parser.add_argument("source", type=Path)
        parsed_import = import_parser.parse_args(argv[1:])
        result = run(_import_snapshot(Settings(), parsed_import.source))
        print("imported " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return
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


def knowledge_worker_main() -> None:
    main([ProcessMode.KNOWLEDGE_WORKER.value])


def plugin_runner_main() -> None:
    main([ProcessMode.PLUGIN_RUNNER.value])
