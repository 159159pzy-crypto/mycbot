"""The plugin broker: the API-resident hub between workers and the runner."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import cast
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mybot.contracts import ModerationDecision, ModerationRequest, PluginManifest, ToolSpec
from mybot.contracts.json import thaw_json_object
from mybot.plugins.config import PluginConfigError, validate_plugin_config
from mybot.plugins.state import PluginRegistration, PluginRegistrationStore

logger = structlog.get_logger("mybot.plugins.broker")

_EVENT_QUEUE_LIMIT = 100
PLUGIN_PROTOCOL_VERSION = 2
_SUPPORTED_PLUGIN_PROTOCOLS = frozenset({1, PLUGIN_PROTOCOL_VERSION})


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str
    protocol_version: int = Field(default=1, ge=1)
    manifests: list[JsonValue]


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    context: dict[str, JsonValue] = Field(default_factory=dict)


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: str
    result: dict[str, JsonValue]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    payload: dict[str, JsonValue]


class UnregisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str


class TaskDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    task_id: str


class ServiceInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str
    plugin_id: str
    service: str
    version: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(slots=True)
class AuditEntry:
    plugin_id: str
    reason: str
    detail: str


type ServiceHandler = Callable[
    [str, dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]
]


@dataclass(slots=True, frozen=True)
class PluginService:
    name: str
    version: int
    capability: str
    handler: ServiceHandler


@dataclass(slots=True)
class _RegisteredTool:
    plugin_id: str
    runner_id: str
    spec: ToolSpec


@dataclass(slots=True)
class _PendingInvocation:
    invocation_id: str
    tool_id: str
    arguments: dict[str, JsonValue]
    context: dict[str, JsonValue]
    future: "asyncio.Future[dict[str, JsonValue]]"


@dataclass(slots=True)
class _RunnerState:
    runner_id: str
    protocol_version: int = 1
    plugins: dict[str, PluginManifest] = field(default_factory=dict[str, PluginManifest])
    work: deque[_PendingInvocation] = field(default_factory=deque[_PendingInvocation])
    events: deque[dict[str, JsonValue]] = field(
        default_factory=lambda: deque(maxlen=_EVENT_QUEUE_LIMIT)
    )
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)


class PluginBroker:
    """Broker with externally persisted runner registration metadata."""

    def __init__(
        self,
        *,
        grants: dict[str, tuple[str, ...]],
        invoke_timeout_seconds: float = 20.0,
        services: tuple[PluginService, ...] = (),
        service_quota_per_minute: int = 30,
        clock: Callable[[], float] = monotonic,
        registrations: PluginRegistrationStore | None = None,
    ) -> None:
        self._grants = grants
        self._invoke_timeout_seconds = invoke_timeout_seconds
        self._runners: dict[str, _RunnerState] = {}
        self._tools: dict[str, _RegisteredTool] = {}
        self._pending: dict[str, _PendingInvocation] = {}
        self._services = {service.name: service for service in services}
        self._service_quota_per_minute = service_quota_per_minute
        self._service_calls: dict[tuple[str, str], deque[float]] = {}
        self._clock = clock
        self._load_errors: dict[str, str] = {}
        self._registrations = registrations
        self.audit_log: list[AuditEntry] = []

    def set_services(self, services: tuple[PluginService, ...]) -> None:
        self._services = {service.name: service for service in services}

    def set_registration_store(self, registrations: PluginRegistrationStore) -> None:
        self._registrations = registrations

    async def restore(self) -> int:
        if self._registrations is None:
            return 0
        restored = 0
        for registration in await self._registrations.load_all():
            try:
                self.register(
                    registration.runner_id,
                    registration.manifests,
                    protocol_version=registration.protocol_version,
                )
            except HTTPException:
                logger.warning(
                    "plugin_registration_restore_rejected",
                    runner_id=registration.runner_id,
                )
                continue
            restored += 1
        return restored

    async def persist(self, runner_id: str) -> None:
        if self._registrations is None:
            return
        state = self._runners.get(runner_id)
        if state is None:
            return
        await self._registrations.save(
            PluginRegistration(
                runner_id=runner_id,
                protocol_version=state.protocol_version,
                manifests=[
                    cast(JsonValue, manifest.model_dump(mode="json"))
                    for manifest in state.plugins.values()
                ],
            )
        )

    async def forget(self, runner_id: str) -> None:
        if self._registrations is not None:
            await self._registrations.remove(runner_id)

    async def aclose(self) -> None:
        if self._registrations is not None:
            await self._registrations.aclose()

    def register(
        self, runner_id: str, manifests: list[JsonValue], *, protocol_version: int = 1
    ) -> list[str]:
        """Validate manifests and adopt their tools; returns accepted plugin ids."""

        if protocol_version not in _SUPPORTED_PLUGIN_PROTOCOLS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"plugin protocol {protocol_version} is unsupported; "
                    f"supported versions are {sorted(_SUPPORTED_PLUGIN_PROTOCOLS)}"
                ),
            )

        validated: list[PluginManifest] = []
        for raw in manifests:
            try:
                manifest = PluginManifest.model_validate(raw)
            except ValidationError as error:
                raise HTTPException(
                    status_code=422, detail=f"invalid plugin manifest: {error.error_count()} errors"
                ) from error
            granted = set(self._grants.get(manifest.id, ()))
            requested = set(manifest.requested_capabilities)
            excessive = requested - granted
            if excessive:
                entry = AuditEntry(
                    plugin_id=manifest.id,
                    reason="capability_refused",
                    detail=f"requested {sorted(excessive)} beyond grants {sorted(granted)}",
                )
                self.audit_log.append(entry)
                logger.warning(
                    "plugin_capability_refused",
                    plugin_id=manifest.id,
                    requested=sorted(requested),
                    granted=sorted(granted),
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"plugin {manifest.id} requests ungranted capabilities",
                )
            for service_name, required_version in manifest.requires.items():
                service = self._services.get(service_name)
                detail: str | None = None
                if service is None:
                    detail = f"required service {service_name}@{required_version} is unavailable"
                elif not _supports_version(str(required_version), service.version):
                    detail = (
                        f"required service {service_name}@{required_version} is incompatible "
                        f"with {service_name}@{service.version}"
                    )
                elif service.capability not in requested:
                    detail = (
                        f"required service {service_name} also requires requested capability "
                        f"{service.capability}"
                    )
                if detail is not None:
                    self._load_errors[manifest.id] = detail
                    self.audit_log.append(
                        AuditEntry(
                            plugin_id=manifest.id,
                            reason="service_negotiation_refused",
                            detail=detail,
                        )
                    )
                    raise HTTPException(status_code=409, detail=detail)
            validated.append(manifest)

        previous = self._runners.get(runner_id)
        state = _RunnerState(runner_id=runner_id, protocol_version=protocol_version)
        if previous is not None:
            self._tools = {
                tool_id: tool
                for tool_id, tool in self._tools.items()
                if tool.runner_id != runner_id
            }
        self._runners[runner_id] = state
        for manifest in validated:
            self._load_errors.pop(manifest.id, None)
            state.plugins[manifest.id] = manifest
            for spec in manifest.tools:
                self._tools[spec.id] = _RegisteredTool(
                    plugin_id=manifest.id, runner_id=runner_id, spec=spec
                )
        logger.info(
            "plugins_registered",
            runner_id=runner_id,
            plugins=[manifest.id for manifest in validated],
            tools=sorted(
                tool_id for tool_id, tool in self._tools.items() if tool.runner_id == runner_id
            ),
        )
        return [manifest.id for manifest in validated]

    def unregister(self, runner_id: str) -> bool:
        state = self._runners.pop(runner_id, None)
        if state is None:
            return False
        self._tools = {
            tool_id: tool
            for tool_id, tool in self._tools.items()
            if tool.runner_id != runner_id
        }
        for pending in tuple(self._pending.values()):
            if pending in state.work and not pending.future.done():
                pending.future.set_exception(RuntimeError("plugin runner disconnected"))
        return True

    def tool_catalog(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "plugin_id": tool.plugin_id,
                "spec": cast(JsonValue, tool.spec.model_dump(mode="json")),
            }
            for tool in self._tools.values()
        ]

    async def invoke(
        self,
        tool_id: str,
        arguments: dict[str, JsonValue],
        context: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        tool = self._tools.get(tool_id)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"no plugin tool named {tool_id}")
        runner = self._runners.get(tool.runner_id)
        if runner is None:
            raise HTTPException(status_code=504, detail="the plugin runner is not connected")
        invocation = _PendingInvocation(
            invocation_id=str(uuid4()),
            tool_id=tool_id,
            arguments=arguments,
            context=context,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[invocation.invocation_id] = invocation
        runner.work.append(invocation)
        runner.wakeup.set()
        try:
            async with asyncio.timeout(self._invoke_timeout_seconds):
                return await invocation.future
        except TimeoutError as error:
            raise HTTPException(
                status_code=504, detail=f"plugin tool {tool_id} timed out"
            ) from error
        finally:
            self._pending.pop(invocation.invocation_id, None)

    async def work(self, runner_id: str, *, wait_seconds: float) -> dict[str, JsonValue]:
        runner = self._runners.get(runner_id)
        if runner is None:
            raise HTTPException(status_code=404, detail="unknown runner; register first")
        if self._registrations is not None:
            await self._registrations.touch(runner_id)
        if not runner.work and not runner.events:
            runner.wakeup.clear()
            try:
                async with asyncio.timeout(wait_seconds):
                    await runner.wakeup.wait()
            except TimeoutError:
                pass
        invocations: list[dict[str, JsonValue]] = []
        while runner.work:
            pending = runner.work.popleft()
            invocations.append(
                {
                    "invocation_id": pending.invocation_id,
                    "tool_id": pending.tool_id,
                    "arguments": pending.arguments,
                    "context": pending.context,
                }
            )
        events: list[dict[str, JsonValue]] = []
        while runner.events:
            events.append(runner.events.popleft())
        return cast(dict[str, JsonValue], {"invocations": invocations, "events": events})

    def post_result(self, invocation_id: str, result: dict[str, JsonValue]) -> bool:
        pending = self._pending.get(invocation_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(result)
        return True

    def post_event(self, kind: str, payload: dict[str, JsonValue]) -> int:
        delivered = 0
        for runner in self._runners.values():
            hooked = any(kind in manifest.event_hooks for manifest in runner.plugins.values())
            if not hooked:
                continue
            runner.events.append(
                _event_for_protocol(kind, payload, protocol_version=runner.protocol_version)
            )
            runner.wakeup.set()
            delivered += 1
        return delivered

    def post_plugin_event(
        self, plugin_id: str, kind: str, payload: dict[str, JsonValue]
    ) -> int:
        for runner in self._runners.values():
            if plugin_id not in runner.plugins:
                continue
            runner.events.append(
                _event_for_protocol(kind, payload, protocol_version=runner.protocol_version)
            )
            runner.wakeup.set()
            return 1
        return 0

    def update_config(self, plugin_id: str, config: dict[str, JsonValue]) -> int:
        manifest = self._manifest(plugin_id)
        try:
            validate_plugin_config(thaw_json_object(manifest.config_schema), config)
        except PluginConfigError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return self.post_plugin_event(
            plugin_id,
            "plugin.config.changed",
            {"plugin_id": plugin_id, "config": cast(JsonValue, config)},
        )

    def task_catalog(self) -> list[dict[str, JsonValue]]:
        tasks: list[dict[str, JsonValue]] = []
        for runner in self._runners.values():
            for manifest in runner.plugins.values():
                tasks.extend(
                    {
                        "plugin_id": manifest.id,
                        "task_id": task.id,
                        "interval_seconds": task.interval_seconds,
                    }
                    for task in manifest.tasks
                )
        return tasks

    def dispatch_task(self, plugin_id: str, task_id: str) -> int:
        manifest = self._manifest(plugin_id)
        if task_id not in {task.id for task in manifest.tasks}:
            raise HTTPException(status_code=404, detail=f"plugin {plugin_id} has no task {task_id}")
        return self.post_plugin_event(
            plugin_id,
            "plugin.task",
            {"plugin_id": plugin_id, "task_id": task_id},
        )

    def service_catalog(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "name": service.name,
                "version": service.version,
                "capability": service.capability,
            }
            for service in sorted(self._services.values(), key=lambda item: item.name)
        ]

    async def invoke_service(
        self,
        *,
        runner_id: str,
        plugin_id: str,
        service: str,
        version: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        runner = self._runners.get(runner_id)
        manifest = runner.plugins.get(plugin_id) if runner is not None else None
        if manifest is None:
            raise HTTPException(status_code=403, detail="runner does not own this plugin")
        required = manifest.requires.get(service)
        definition = self._services.get(service)
        if required is None or definition is None:
            raise HTTPException(status_code=404, detail=f"service {service} was not negotiated")
        if not _supports_version(version, definition.version) or not _supports_version(
            str(required), definition.version
        ):
            raise HTTPException(status_code=409, detail=f"service {service} version mismatch")
        granted = set(self._grants.get(plugin_id, ()))
        if definition.capability not in granted:
            self.audit_log.append(
                AuditEntry(plugin_id, "service_capability_refused", service)
            )
            raise HTTPException(status_code=403, detail="service capability is not granted")
        self._consume_service_quota(plugin_id, service)
        result = await definition.handler(plugin_id, arguments)
        self.audit_log.append(AuditEntry(plugin_id, "service_invoked", f"{service}@{version}"))
        return result

    async def moderate(self, request: ModerationRequest) -> ModerationDecision:
        candidates = sorted(
            (
                tool
                for tool in self._tools.values()
                if "moderation" in tool.spec.capabilities
            ),
            key=lambda item: item.spec.id,
        )
        if not candidates:
            raise HTTPException(status_code=503, detail="no moderation plugin is registered")
        result = await self.invoke(
            candidates[0].spec.id,
            request.model_dump(mode="json"),
            {"system": "moderation"},
        )
        if result.get("ok") is not True or not isinstance(result.get("data"), dict):
            raise HTTPException(status_code=502, detail="moderation plugin returned an error")
        return ModerationDecision.model_validate(result["data"])

    def _consume_service_quota(self, plugin_id: str, service: str) -> None:
        now = self._clock()
        calls = self._service_calls.setdefault((plugin_id, service), deque())
        while calls and now - calls[0] >= 60.0:
            calls.popleft()
        if len(calls) >= self._service_quota_per_minute:
            self.audit_log.append(
                AuditEntry(plugin_id, "service_quota_exceeded", service)
            )
            raise HTTPException(status_code=429, detail=f"service {service} quota exceeded")
        calls.append(now)

    def _manifest(self, plugin_id: str) -> PluginManifest:
        for runner in self._runners.values():
            manifest = runner.plugins.get(plugin_id)
            if manifest is not None:
                return manifest
        raise HTTPException(status_code=404, detail=f"unknown plugin {plugin_id}")

    def manifest(self, plugin_id: str) -> PluginManifest | None:
        try:
            return self._manifest(plugin_id)
        except HTTPException:
            return None

    def health(self) -> dict[str, JsonValue]:
        plugins: list[JsonValue] = []
        for runner in self._runners.values():
            for manifest in runner.plugins.values():
                plugins.append(
                    {
                        "id": manifest.id,
                        "version": manifest.version,
                        "runner_id": runner.runner_id,
                        "tools": [spec.id for spec in manifest.tools],
                        "event_hooks": list(manifest.event_hooks),
                        "tasks": [task.model_dump(mode="json") for task in manifest.tasks],
                        "granted_capabilities": list(self._grants.get(manifest.id, ())),
                        "config_schema": cast(JsonValue, thaw_json_object(manifest.config_schema)),
                        "requires": cast(JsonValue, thaw_json_object(manifest.requires)),
                    }
                )
        return {
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
            "runners": len(self._runners),
            "runner_protocol_versions": {
                runner_id: state.protocol_version for runner_id, state in self._runners.items()
            },
            "plugins": plugins,
            "services": cast(JsonValue, self.service_catalog()),
            "load_errors": cast(
                JsonValue,
                [
                    {"plugin_id": plugin_id, "detail": detail}
                    for plugin_id, detail in sorted(self._load_errors.items())
                ],
            ),
        }


def create_broker_router(broker: PluginBroker) -> APIRouter:
    router = APIRouter(prefix="/plugin-broker", tags=["plugins"])

    @router.post("/register")
    async def register(request: RegisterRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        accepted = broker.register(
            request.runner_id,
            request.manifests,
            protocol_version=request.protocol_version,
        )
        await broker.persist(request.runner_id)
        return {"accepted": cast(JsonValue, accepted)}

    @router.post("/unregister")
    async def unregister(request: UnregisterRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        removed = broker.unregister(request.runner_id)
        await broker.forget(request.runner_id)
        return {"removed": removed}

    @router.get("/tools")
    def tools() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"tools": cast(JsonValue, broker.tool_catalog())}

    @router.post("/invoke")
    async def invoke(request: InvokeRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        result = await broker.invoke(request.tool_id, request.arguments, request.context)
        return {"result": cast(JsonValue, result)}

    @router.get("/work")
    async def work(runner_id: str, wait_seconds: float = 20.0) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return await broker.work(runner_id, wait_seconds=min(wait_seconds, 55.0))

    @router.post("/result")
    def result(request: ResultRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        accepted = broker.post_result(request.invocation_id, request.result)
        return {"accepted": accepted}

    @router.post("/events")
    def events(request: EventRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        delivered = broker.post_event(request.kind, request.payload)
        return {"delivered": delivered}

    @router.get("/tasks")
    def tasks() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"tasks": cast(JsonValue, broker.task_catalog())}

    @router.post("/tasks/dispatch")
    def dispatch_task(request: TaskDispatchRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"delivered": broker.dispatch_task(request.plugin_id, request.task_id)}

    @router.get("/services")
    def services() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"services": cast(JsonValue, broker.service_catalog())}

    @router.post("/services/invoke")
    async def invoke_service(request: ServiceInvokeRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        result = await broker.invoke_service(
            runner_id=request.runner_id,
            plugin_id=request.plugin_id,
            service=request.service,
            version=request.version,
            arguments=request.arguments,
        )
        return {"result": cast(JsonValue, result)}

    @router.post("/moderate")
    async def moderate(request: ModerationRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        result = await broker.moderate(request)
        return {"result": cast(JsonValue, result.model_dump(mode="json"))}

    @router.get("/health")
    def health() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return broker.health()

    return router


def _event_for_protocol(
    kind: str, payload: dict[str, JsonValue], *, protocol_version: int
) -> dict[str, JsonValue]:
    if protocol_version >= 2:
        return {"kind": kind, "payload": payload}
    projected = dict(payload)
    envelope = projected.get("envelope")
    if isinstance(envelope, dict):
        compatible_envelope = dict(cast(dict[str, JsonValue], envelope))
        compatible_envelope.pop("trace_id", None)
        compatible_envelope.pop("ephemeral", None)
        segments = compatible_envelope.get("segments")
        if isinstance(segments, list):
            compatible_envelope["segments"] = cast(
                JsonValue,
                [_legacy_segment(item) for item in cast(list[object], segments)],
            )
        projected["envelope"] = cast(JsonValue, compatible_envelope)
    return {"kind": kind, "payload": projected}


def _supports_version(requested: str, available: int) -> bool:
    normalized = requested.strip()
    if normalized.startswith("^"):
        normalized = normalized[1:]
    major = normalized.split(".", 1)[0]
    return major.isdigit() and int(major) == available


def _legacy_segment(raw: object) -> JsonValue:
    if not isinstance(raw, dict):
        return cast(JsonValue, raw)
    segment = cast(dict[str, object], raw)
    segment_type = segment.get("type")
    if segment_type == "at":
        label = segment.get("display_name") or segment.get("target_id") or "user"
        return {"type": "text", "text": f"[@{label}]"}
    if segment_type == "sticker":
        label = segment.get("name") or segment.get("id") or "sticker"
        return {"type": "text", "text": f"[sticker: {label}]"}
    if segment_type == "voice":
        return {"type": "text", "text": "[voice message]"}
    return cast(JsonValue, dict(segment))
