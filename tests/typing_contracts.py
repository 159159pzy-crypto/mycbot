from typing import assert_type

from mybot.contracts import MessageEnvelope, PluginManifest, ToolError, ToolResult, ToolSpec
from mybot.contracts.json import FrozenJsonObject, FrozenJsonValue


def assert_immutable_public_json_types(
    message: MessageEnvelope,
    tool: ToolSpec,
    plugin: PluginManifest,
    result: ToolResult,
    error: ToolError,
) -> None:
    assert_type(message.raw_ref, FrozenJsonValue | None)
    assert_type(tool.input_schema, FrozenJsonObject)
    assert_type(plugin.config_schema, FrozenJsonObject)
    assert_type(result.data, FrozenJsonObject | None)
    assert_type(error.details, FrozenJsonObject)
