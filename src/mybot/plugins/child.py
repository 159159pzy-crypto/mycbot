"""One isolated plugin child process managed by PluginSupervisorService."""

import os

import httpx
from pydantic import JsonValue

from mybot.asyncio_compat import run
from mybot.contracts.json import parse_json
from mybot.plugins.runner import PluginRunnerService, load_plugin_entrypoint
from mybot.runtime import ProcessMode, run_process


async def _main() -> None:
    broker_url = os.environ["MYBOT_PLUGIN_CHILD_BROKER_URL"]
    entrypoint = os.environ["MYBOT_PLUGIN_CHILD_ENTRYPOINT"]
    runner_id = os.environ["MYBOT_PLUGIN_CHILD_RUNNER_ID"]
    python_path = os.environ.get("MYBOT_PLUGIN_CHILD_PYTHON_PATH") or None
    raw_config = os.environ.get("MYBOT_PLUGIN_CHILD_CONFIG", "{}")
    decoded = parse_json(raw_config)
    config: dict[str, JsonValue] = decoded if isinstance(decoded, dict) else {}
    plugin = load_plugin_entrypoint(entrypoint, python_path)
    await plugin.update_config(config)
    client = httpx.AsyncClient()
    service = PluginRunnerService(
        broker_url=broker_url,
        plugins=[plugin],
        client=client,
        runner_id=runner_id,
    )
    try:
        await run_process(ProcessMode.PLUGIN_RUNNER, service=service)
    finally:
        try:
            await client.post(
                f"{broker_url.rstrip('/')}/plugin-broker/unregister",
                json={"runner_id": runner_id},
                timeout=3.0,
            )
        finally:
            await client.aclose()
def main() -> None:
    run(_main())


if __name__ == "__main__":
    main()
