"""The plugin authoring surface: a manifest plus plain handler callables."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from mybot.contracts import PluginManifest

type ToolHandler = Callable[
    [Mapping[str, JsonValue]],
    Awaitable[dict[str, JsonValue]] | dict[str, JsonValue],
]
type EventHandler = Callable[[Mapping[str, JsonValue]], Awaitable[None] | None]


class PluginToolError(RuntimeError):
    """Raise from a tool handler to return a structured, user-safe failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class SimplePlugin:
    """A manifest with tool handlers; the runner drives everything else."""

    manifest: PluginManifest
    tool_handlers: Mapping[str, ToolHandler] = field(
        default_factory=dict[str, ToolHandler]
    )
    event_handler: EventHandler | None = None

    def __post_init__(self) -> None:
        declared = {spec.id for spec in self.manifest.tools}
        handled = set(self.tool_handlers)
        if declared != handled:
            raise ValueError(
                f"plugin {self.manifest.id} declares tools {sorted(declared)} "
                f"but handles {sorted(handled)}"
            )

    async def run_tool(
        self, tool_id: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        handler = self.tool_handlers[tool_id]
        result = handler(arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    async def deliver_event(self, event: Mapping[str, JsonValue]) -> None:
        if self.event_handler is None:
            return
        outcome = self.event_handler(event)
        if inspect.isawaitable(outcome):
            await outcome
