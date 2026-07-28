"""The plugin-runner lifecycle: load, register, poll, execute, report."""

import asyncio
import importlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

import httpx
import structlog
from pydantic import JsonValue

from mybot.adapters.payload import as_string_mapping, get_str
from mybot.plugins.broker import PLUGIN_PROTOCOL_VERSION
from mybot.plugins.sdk import PluginToolError, SimplePlugin
from mybot.runtime import ProcessMode
from mybot.settings import Settings

logger = structlog.get_logger("mybot.plugins.runner")


@dataclass(slots=True)
class BrokerServiceClient:
    client: httpx.AsyncClient
    broker_url: str
    runner_id: str
    plugin_id: str

    async def invoke(
        self, service: str, version: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        try:
            response = await self.client.post(
                f"{self.broker_url.rstrip('/')}/plugin-broker/services/invoke",
                json={
                    "runner_id": self.runner_id,
                    "plugin_id": self.plugin_id,
                    "service": service,
                    "version": version,
                    "arguments": dict(arguments),
                },
                timeout=30.0,
            )
        except httpx.HTTPError as error:
            raise PluginToolError(
                "service_unreachable", f"service broker failed: {type(error).__name__}"
            ) from error
        if response.status_code >= 400:
            raise PluginToolError(
                "service_refused", f"service broker returned HTTP {response.status_code}"
            )
        body = as_string_mapping(response.json()) or {}
        result = as_string_mapping(body.get("result"))
        if result is None:
            raise PluginToolError("service_invalid_result", "service returned invalid data")
        return dict(cast(Mapping[str, JsonValue], result))


def load_plugins(config_json: str) -> list[SimplePlugin]:
    """Import plugins from config entrypoints; one bad entry never aborts the rest."""

    try:
        decoded = json.loads(config_json)
    except ValueError:
        logger.warning("plugin_config_not_json")
        return []
    config = as_string_mapping(decoded) or {}
    entries = config.get("plugins")
    if not isinstance(entries, list):
        return []
    plugins: list[SimplePlugin] = []
    for raw in cast(list[object], entries):
        if not isinstance(raw, str) or ":" not in raw:
            logger.warning("plugin_entrypoint_invalid", entry=str(raw))
            continue
        try:
            plugin = load_plugin_entrypoint(raw)
        except Exception:
            logger.exception("plugin_import_failed", entry=raw)
            continue
        plugins.append(plugin)
        logger.info("plugin_loaded", plugin_id=plugin.manifest.id, version=plugin.manifest.version)
    return plugins


def load_plugin_entrypoint(entrypoint: str, python_path: str | None = None) -> SimplePlugin:
    if ":" not in entrypoint:
        raise ValueError("plugin entrypoint must be module:attribute")
    if python_path and python_path not in sys.path:
        sys.path.insert(0, python_path)
    importlib.invalidate_caches()
    module_name, _, attribute = entrypoint.partition(":")
    module = importlib.import_module(module_name)
    plugin = getattr(module, attribute)
    if not isinstance(plugin, SimplePlugin):
        raise TypeError("plugin entrypoint does not expose a SimplePlugin")
    return plugin


@dataclass(slots=True)
class PluginRunnerService:
    """LifecycleService serving loaded plugins against the broker."""

    broker_url: str
    plugins: list[SimplePlugin]
    client: httpx.AsyncClient
    poll_wait_seconds: float = 20.0
    call_timeout_seconds: float = 15.0
    result_max_chars: int = 16_000
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    runner_id: str = field(default_factory=lambda: f"runner-{uuid4()}")

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        for plugin in self.plugins:
            plugin.bind_services(
                BrokerServiceClient(
                    client=self.client,
                    broker_url=self.broker_url,
                    runner_id=self.runner_id,
                    plugin_id=plugin.manifest.id,
                )
            )
        logger.info(
            "plugin_runner_started",
            mode=mode.value,
            plugins=[plugin.manifest.id for plugin in self.plugins],
        )
        if not self.plugins:
            await stop_event.wait()
            return
        try:
            while not stop_event.is_set():
                registered = await self._register_with_retry(stop_event)
                if not registered:
                    return
                await self._serve(stop_event)
        finally:
            logger.info("plugin_runner_stopped", mode=mode.value)

    async def _register_with_retry(self, stop_event: asyncio.Event) -> bool:
        backoff = self.reconnect_initial_seconds
        while not stop_event.is_set():
            try:
                response = await self.client.post(
                    f"{self.broker_url.rstrip('/')}/plugin-broker/register",
                    json={
                        "runner_id": self.runner_id,
                        "protocol_version": PLUGIN_PROTOCOL_VERSION,
                        "manifests": [
                            plugin.manifest.model_dump(mode="json") for plugin in self.plugins
                        ],
                    },
                    timeout=10.0,
                )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as error:
                logger.warning("plugin_registration_unreachable", error=type(error).__name__)
                await _wait(stop_event, backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)
                continue
            if response.status_code == 200:
                logger.info("plugin_registration_accepted", runner_id=self.runner_id)
                return True
            logger.error(
                "plugin_registration_refused",
                status=response.status_code,
                detail=response.text[:500],
            )
            # A policy refusal (403/422) will not fix itself; idle until stopped.
            await stop_event.wait()
            return False
        return False

    async def _serve(self, stop_event: asyncio.Event) -> None:
        backoff = self.reconnect_initial_seconds
        while not stop_event.is_set():
            try:
                response = await self.client.get(
                    f"{self.broker_url.rstrip('/')}/plugin-broker/work",
                    params={
                        "runner_id": self.runner_id,
                        "wait_seconds": self.poll_wait_seconds,
                    },
                    timeout=self.poll_wait_seconds + 10.0,
                )
                if response.status_code == 404:
                    logger.warning("plugin_runner_unknown_reregistering")
                    return  # outer loop re-registers (broker restarted)
                response.raise_for_status()
                backoff = self.reconnect_initial_seconds
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as error:
                logger.warning("plugin_poll_failed", error=type(error).__name__)
                await _wait(stop_event, backoff)
                backoff = min(backoff * 2, self.reconnect_max_seconds)
                continue
            work = as_string_mapping(response.json()) or {}
            for raw_event in _as_list(work.get("events")):
                await self._deliver_event(raw_event)
            for raw_invocation in _as_list(work.get("invocations")):
                await self._execute(raw_invocation)
            await asyncio.sleep(0)

    async def _deliver_event(self, raw_event: object) -> None:
        event = as_string_mapping(raw_event)
        if event is None:
            return
        kind = get_str(event, "kind") or ""
        payload = as_string_mapping(event.get("payload")) or {}
        target = get_str(payload, "plugin_id")
        for plugin in self.plugins:
            if target is not None and target != plugin.manifest.id:
                continue
            if kind == "plugin.config.changed":
                config = as_string_mapping(payload.get("config")) or {}
                try:
                    await plugin.update_config(cast(Mapping[str, JsonValue], config))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("plugin_config_handler_failed", plugin_id=plugin.manifest.id)
                continue
            if kind == "plugin.task":
                task_id = get_str(payload, "task_id")
                if task_id is None or task_id not in plugin.task_handlers:
                    continue
                try:
                    await plugin.run_task(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "plugin_task_handler_failed",
                        plugin_id=plugin.manifest.id,
                        task_id=task_id,
                    )
                continue
            if kind not in plugin.manifest.event_hooks:
                continue
            try:
                await plugin.deliver_event({"kind": kind, **cast(Mapping[str, JsonValue], payload)})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("plugin_event_handler_failed", plugin_id=plugin.manifest.id)

    async def _execute(self, raw_invocation: object) -> None:
        invocation = as_string_mapping(raw_invocation)
        if invocation is None:
            return
        invocation_id = get_str(invocation, "invocation_id")
        tool_id = get_str(invocation, "tool_id")
        if invocation_id is None or tool_id is None:
            return
        arguments = as_string_mapping(invocation.get("arguments")) or {}
        result = await self._run_tool(tool_id, cast(Mapping[str, JsonValue], arguments))
        try:
            await self.client.post(
                f"{self.broker_url.rstrip('/')}/plugin-broker/result",
                json={"invocation_id": invocation_id, "result": result},
                timeout=10.0,
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            logger.warning("plugin_result_post_failed", error=type(error).__name__)

    async def _run_tool(
        self, tool_id: str, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        plugin = next(
            (candidate for candidate in self.plugins if tool_id in candidate.tool_handlers),
            None,
        )
        if plugin is None:
            return _failure("unknown_tool", f"this runner does not serve {tool_id}")
        try:
            async with asyncio.timeout(self.call_timeout_seconds):
                data = await plugin.run_tool(tool_id, arguments)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("plugin_tool_timed_out", tool_id=tool_id)
            return _failure(
                "timeout",
                f"{tool_id} exceeded {self.call_timeout_seconds:g}s",
                retryable=True,
            )
        except PluginToolError as error:
            return _failure(error.code, str(error))
        except Exception as error:
            logger.exception("plugin_tool_crashed", tool_id=tool_id, plugin_id=plugin.manifest.id)
            return _failure("tool_error", type(error).__name__)
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > self.result_max_chars:
            return _failure(
                "result_too_large",
                f"{tool_id} produced {len(encoded)} chars (limit {self.result_max_chars})",
            )
        return {"ok": True, "data": data, "error": None}


def create_plugin_runner_service(settings: Settings):  # type: ignore[no-untyped-def]
    if settings.plugin_broker_url is None:
        return None
    from pathlib import Path

    from mybot.plugins.control import PluginControlStore
    from mybot.plugins.supervisor import (
        PluginSupervisorService,
        SubprocessPluginInspector,
        SubprocessPluginSpawner,
    )

    return PluginSupervisorService(
        broker_url=settings.plugin_broker_url,
        config_json=settings.plugin_config,
        store=PluginControlStore(Path(settings.plugin_data_dir)),
        client=httpx.AsyncClient(),
        inspector=SubprocessPluginInspector(),
        spawner=SubprocessPluginSpawner(settings.plugin_broker_url),
        poll_seconds=settings.plugin_supervisor_poll_seconds,
        crash_limit=settings.plugin_crash_limit,
        crash_window_seconds=settings.plugin_crash_window_seconds,
    )


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, JsonValue]:
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message, "retryable": retryable, "details": {}},
    }


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop_event.wait()
    except TimeoutError:
        pass
