"""The plugin authoring surface: a manifest plus plain handler callables."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import JsonValue

from mybot.contracts import PluginManifest

type ToolHandler = Callable[
    [Mapping[str, JsonValue]],
    Awaitable[dict[str, JsonValue]] | dict[str, JsonValue],
]
type EventHandler = Callable[[Mapping[str, JsonValue]], Awaitable[None] | None]
type TaskHandler = Callable[[], Awaitable[None] | None]
type ConfigHandler = Callable[[Mapping[str, JsonValue]], Awaitable[None] | None]


class PluginServiceClient(Protocol):
    async def invoke(
        self, service: str, version: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]: ...


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
    task_handlers: Mapping[str, TaskHandler] = field(
        default_factory=dict[str, TaskHandler]
    )
    config_handler: ConfigHandler | None = None
    services: PluginServiceClient | None = None

    def __post_init__(self) -> None:
        declared = {spec.id for spec in self.manifest.tools}
        handled = set(self.tool_handlers)
        if declared != handled:
            raise ValueError(
                f"plugin {self.manifest.id} declares tools {sorted(declared)} "
                f"but handles {sorted(handled)}"
            )
        declared_tasks = {task.id for task in self.manifest.tasks}
        handled_tasks = set(self.task_handlers)
        if declared_tasks != handled_tasks:
            raise ValueError(
                f"plugin {self.manifest.id} declares tasks {sorted(declared_tasks)} "
                f"but handles {sorted(handled_tasks)}"
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

    async def run_task(self, task_id: str) -> None:
        outcome = self.task_handlers[task_id]()
        if inspect.isawaitable(outcome):
            await outcome

    async def update_config(self, config: Mapping[str, JsonValue]) -> None:
        if self.config_handler is None:
            return
        outcome = self.config_handler(config)
        if inspect.isawaitable(outcome):
            await outcome

    def bind_services(self, client: PluginServiceClient) -> None:
        self.services = client

    async def call_service(
        self, service: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        required = self.manifest.requires.get(service)
        if not isinstance(required, str):
            raise PluginToolError("service_not_declared", f"{service} is not declared")
        if self.services is None:
            raise PluginToolError("service_unavailable", "plugin services are not bound")
        return await self.services.invoke(service, required, arguments)
