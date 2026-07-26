"""The reference plugin: a dice roller tool plus a message event counter."""

import random
from collections.abc import Mapping

from pydantic import JsonValue

from mybot.contracts import PluginManifest, ToolRisk, ToolSpec
from mybot.plugins.sdk import PluginToolError, SimplePlugin

_MANIFEST = PluginManifest(
    id="example.dice",
    version="1.0.0",
    entrypoint="mybot.plugins.examples.dice:PLUGIN",
    event_hooks=("message",),
    tools=(
        ToolSpec.model_validate(
            {
                "id": "roll_dice",
                "description": (
                    "Roll dice and return the individual results and their total. "
                    "Use for games, random choices, or when the user asks for a roll."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sides": {"type": "integer", "minimum": 2, "maximum": 1000},
                        "count": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "additionalProperties": False,
                },
                "read_only": True,
                "idempotent": False,
                "risk": ToolRisk.NONE,
                "capabilities": (),
                "approval_required": False,
            }
        ),
    ),
)

_seen_events = 0


def _roll(arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    sides = arguments.get("sides", 6)
    count = arguments.get("count", 1)
    if not isinstance(sides, int) or not 2 <= sides <= 1000:
        raise PluginToolError("invalid_arguments", "sides must be an integer in 2..1000")
    if not isinstance(count, int) or not 1 <= count <= 10:
        raise PluginToolError("invalid_arguments", "count must be an integer in 1..10")
    rolls = [random.randint(1, sides) for _ in range(count)]
    result: dict[str, JsonValue] = {
        "sides": sides,
        "count": count,
        "rolls": list(rolls),
        "total": sum(rolls),
    }
    return result


def _on_event(event: Mapping[str, JsonValue]) -> None:
    global _seen_events
    _seen_events += 1


def seen_events() -> int:
    return _seen_events


PLUGIN = SimplePlugin(
    manifest=_MANIFEST,
    tool_handlers={"roll_dice": _roll},
    event_handler=_on_event,
)
