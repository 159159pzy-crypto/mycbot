"""Short-lived subprocess used to inspect one plugin without importing it in the supervisor."""

import os

from mybot.plugins.runner import load_plugin_entrypoint


def main() -> None:
    plugin = load_plugin_entrypoint(
        os.environ["MYBOT_PLUGIN_PROBE_ENTRYPOINT"],
        os.environ.get("MYBOT_PLUGIN_PROBE_PYTHON_PATH") or None,
    )
    print(plugin.manifest.model_dump_json())


if __name__ == "__main__":
    main()
