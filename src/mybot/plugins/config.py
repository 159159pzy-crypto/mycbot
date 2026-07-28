"""Small fail-closed JSON Schema subset for plugin configuration."""

from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue


class PluginConfigError(ValueError):
    pass


def validate_plugin_config(schema: Mapping[str, object], value: JsonValue) -> None:
    _validate(schema, value, path="config")


def _validate(schema: Mapping[str, object], value: JsonValue, *, path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(expected, value):
        raise PluginConfigError(f"{path} must be {expected}")
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and value not in enum:
        raise PluginConfigError(f"{path} must be one of the declared enum values")
    if isinstance(value, dict):
        properties = schema.get("properties")
        property_map: Mapping[str, object] = (
            cast(Mapping[str, object], properties)
            if isinstance(properties, Mapping)
            else {}
        )
        required = schema.get("required")
        if isinstance(required, (list, tuple)):
            for key in cast(Sequence[object], required):
                if isinstance(key, str) and key not in value:
                    raise PluginConfigError(f"{path}.{key} is required")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_schema = property_map.get(key)
            if isinstance(child_schema, Mapping):
                _validate(
                    cast(Mapping[str, object], child_schema),
                    child,
                    path=f"{path}.{key}",
                )
            elif additional is False:
                raise PluginConfigError(f"{path}.{key} is not allowed")
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(value):
                _validate(
                    cast(Mapping[str, object], items),
                    child,
                    path=f"{path}[{index}]",
                )
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise PluginConfigError(f"{path} is shorter than minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise PluginConfigError(f"{path} is longer than maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise PluginConfigError(f"{path} is below minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise PluginConfigError(f"{path} is above maximum")


def _matches_type(expected: str, value: JsonValue) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise PluginConfigError(f"unsupported JSON Schema type {expected}")


__all__ = ["PluginConfigError", "validate_plugin_config"]
