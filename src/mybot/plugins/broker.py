"""The plugin broker: the API-resident hub between workers and the runner."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mybot.contracts import PluginManifest, ToolSpec
from mybot.contracts.json import thaw_json_object

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


@dataclass(slots=True)
class AuditEntry:
    plugin_id: str
    reason: str
    detail: str


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
    """In-process broker state; one API process owns it (documented stance)."""

    def __init__(
        self,
        *,
        grants: dict[str, tuple[str, ...]],
        invoke_timeout_seconds: float = 20.0,
    ) -> None:
        self._grants = grants
        self._invoke_timeout_seconds = invoke_timeout_seconds
        self._runners: dict[str, _RunnerState] = {}
        self._tools: dict[str, _RegisteredTool] = {}
        self._pending: dict[str, _PendingInvocation] = {}
        self.audit_log: list[AuditEntry] = []

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
                        "granted_capabilities": list(self._grants.get(manifest.id, ())),
                        "config_schema": cast(JsonValue, thaw_json_object(manifest.config_schema)),
                    }
                )
        return {
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
            "runners": len(self._runners),
            "runner_protocol_versions": {
                runner_id: state.protocol_version for runner_id, state in self._runners.items()
            },
            "plugins": plugins,
        }


def create_broker_router(broker: PluginBroker) -> APIRouter:
    router = APIRouter(prefix="/plugin-broker", tags=["plugins"])

    @router.post("/register")
    def register(request: RegisterRequest) -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        accepted = broker.register(
            request.runner_id,
            request.manifests,
            protocol_version=request.protocol_version,
        )
        return {"accepted": cast(JsonValue, accepted)}

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
