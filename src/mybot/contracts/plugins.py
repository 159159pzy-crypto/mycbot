"""Install-time plugin declaration contract."""

import re
from typing import cast

from pydantic import Field, field_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.json import FrozenJsonObject, FrozenJsonObjectValue, freeze_json_object
from mybot.contracts.messages import Platform
from mybot.contracts.tools import ToolSpec

SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def default_config_schema() -> FrozenJsonObject:
    return freeze_json_object({"type": "object", "additionalProperties": False})


class PluginTaskSpec(FrozenModel):
    id: NonEmptyStr = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    interval_seconds: int = Field(default=3600, ge=1, le=2_592_000)


class PluginManifest(FrozenModel):
    id: NonEmptyStr = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
    version: NonEmptyStr
    entrypoint: NonEmptyStr = Field(
        pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
    )
    event_hooks: tuple[NonEmptyStr, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    tasks: tuple[PluginTaskSpec, ...] = ()
    config_schema: FrozenJsonObjectValue = Field(
        default_factory=default_config_schema,
        validate_default=False,
    )
    platforms: frozenset[Platform] = frozenset()
    requested_capabilities: tuple[NonEmptyStr, ...] = ()
    requires: FrozenJsonObjectValue = Field(
        default_factory=lambda: freeze_json_object({}),
        validate_default=False,
    )

    @field_validator("tasks", mode="before")
    @classmethod
    def normalize_tasks(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            items = cast(list[object] | tuple[object, ...], value)
            return [
                {"id": item, "interval_seconds": 3600} if isinstance(item, str) else item
                for item in items
            ]
        return value

    @field_validator("requires")
    @classmethod
    def validate_requires(cls, value: FrozenJsonObject) -> FrozenJsonObject:
        for service, version in value.items():
            if re.fullmatch(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", service) is None:
                raise ValueError("requires service names must be stable identifiers")
            if not isinstance(version, str) or not version.strip():
                raise ValueError("requires versions must be non-empty strings")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if SEMVER_PATTERN.fullmatch(value) is None:
            raise ValueError("version must be a semantic version")
        return value
