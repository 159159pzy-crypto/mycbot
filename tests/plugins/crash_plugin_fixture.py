"""A deliberately misbehaving plugin used by crash-isolation tests."""

from collections.abc import Mapping

from pydantic import JsonValue

from mybot.contracts import PluginManifest, ToolRisk, ToolSpec
from mybot.plugins.sdk import SimplePlugin

_MANIFEST = PluginManifest(
    id="test.crasher",
    version="0.1.0",
    entrypoint="crash_plugin_fixture:PLUGIN",
    tools=(
        ToolSpec.model_validate(
            {
                "id": "crash_now",
                "description": "Always raises",
                "input_schema": {"type": "object", "properties": {}},
                "read_only": True,
                "idempotent": True,
                "risk": ToolRisk.NONE,
                "capabilities": (),
                "approval_required": False,
            }
        ),
    ),
)


def _crash(arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    raise RuntimeError("deliberate crash for isolation testing")


PLUGIN = SimplePlugin(manifest=_MANIFEST, tool_handlers={"crash_now": _crash})
