"""Install-time plugin declaration contract."""

import re

from pydantic import Field, field_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.contracts.json import FrozenJsonObjectValue
from mybot.contracts.messages import Platform
from mybot.contracts.tools import ToolSpec

SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class PluginManifest(FrozenModel):
    id: NonEmptyStr = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
    version: NonEmptyStr
    entrypoint: NonEmptyStr = Field(
        pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
    )
    event_hooks: tuple[NonEmptyStr, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    tasks: tuple[NonEmptyStr, ...] = ()
    config_schema: FrozenJsonObjectValue = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )
    platforms: frozenset[Platform] = frozenset()
    requested_capabilities: tuple[NonEmptyStr, ...] = ()

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if SEMVER_PATTERN.fullmatch(value) is None:
            raise ValueError("version must be a semantic version")
        return value
