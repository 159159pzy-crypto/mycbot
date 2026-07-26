"""Worker-side view of plugin tools served by the broker."""

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import cast

import httpx
import structlog
from pydantic import JsonValue, ValidationError

from mybot.adapters.payload import as_string_mapping, get_str
from mybot.contracts import ToolContext, ToolError, ToolResult, ToolSpec
from mybot.tools import Tool, ToolRegistry, to_openai_tool

logger = structlog.get_logger("mybot.tools.plugins")


@dataclass(slots=True)
class PluginToolProxy:
    """A Tool that relays execution through the broker's invoke endpoint."""

    _spec: ToolSpec
    plugin_id: str
    client: httpx.AsyncClient
    broker_url: str
    invoke_timeout_seconds: float = 20.0

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        try:
            response = await self.client.post(
                f"{self.broker_url.rstrip('/')}/plugin-broker/invoke",
                json={
                    "tool_id": self._spec.id,
                    "arguments": dict(arguments),
                    "context": context.model_dump(mode="json"),
                },
                timeout=self.invoke_timeout_seconds + 5.0,
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            return _fail(
                "plugin_unreachable",
                f"broker call failed: {type(error).__name__}",
                retryable=True,
            )
        if response.status_code == 404:
            return _fail("unknown_tool", f"{self._spec.id} is no longer registered")
        if response.status_code == 504:
            return _fail("timeout", f"{self._spec.id} timed out in the runner", retryable=True)
        if response.status_code >= 400:
            return _fail(
                "plugin_error", f"broker returned HTTP {response.status_code}"
            )
        body = as_string_mapping(response.json()) or {}
        try:
            return ToolResult.model_validate(body.get("result"))
        except ValidationError:
            return _fail("invalid_result", f"{self.plugin_id} returned a malformed result")


@dataclass(slots=True)
class BrokerToolCatalog:
    """Static tools merged with the broker's live plugin tools (TTL-cached)."""

    static: ToolRegistry
    client: httpx.AsyncClient
    broker_url: str
    ttl_seconds: float = 30.0
    invoke_timeout_seconds: float = 20.0
    clock: Callable[[], float] = field(default=monotonic)
    _plugin_tools: dict[str, PluginToolProxy] = field(
        default_factory=dict[str, "PluginToolProxy"], init=False
    )
    _fetched_at: float | None = field(default=None, init=False)

    async def refresh(self) -> None:
        """Fetch the broker catalog when the cache has expired; outages degrade."""

        now = self.clock()
        if self._fetched_at is not None and now - self._fetched_at < self.ttl_seconds:
            return
        try:
            response = await self.client.get(
                f"{self.broker_url.rstrip('/')}/plugin-broker/tools", timeout=5.0
            )
            response.raise_for_status()
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            logger.warning("plugin_catalog_refresh_failed", error=type(error).__name__)
            self._fetched_at = now  # back off for one TTL, keep the last good view
            return
        payload = as_string_mapping(response.json()) or {}
        raw_tools = payload.get("tools")
        proxies: dict[str, PluginToolProxy] = {}
        if isinstance(raw_tools, list):
            for raw in cast(list[object], raw_tools):
                entry = as_string_mapping(raw)
                if entry is None:
                    continue
                try:
                    spec = ToolSpec.model_validate(entry.get("spec"))
                except ValidationError:
                    logger.warning("plugin_catalog_entry_invalid")
                    continue
                if self.static.get(spec.id) is not None:
                    logger.warning("plugin_tool_shadows_builtin", tool_id=spec.id)
                    continue
                proxies[spec.id] = PluginToolProxy(
                    _spec=spec,
                    plugin_id=get_str(entry, "plugin_id") or "unknown",
                    client=self.client,
                    broker_url=self.broker_url,
                    invoke_timeout_seconds=self.invoke_timeout_seconds,
                )
        self._plugin_tools = proxies
        self._fetched_at = now

    def get(self, tool_id: str) -> Tool | None:
        static = self.static.get(tool_id)
        if static is not None:
            return static
        return self._plugin_tools.get(tool_id)

    def available(self, granted: Collection[str]) -> list[Tool]:
        granted_set = set(granted)
        merged: list[Tool] = list(self.static.available(granted))
        merged.extend(
            proxy
            for proxy in self._plugin_tools.values()
            if set(proxy.spec.capabilities) <= granted_set
        )
        return merged

    def openai_tools(self, granted: Collection[str]) -> list[dict[str, object]]:
        return [to_openai_tool(tool.spec) for tool in self.available(granted)]


@dataclass(slots=True)
class BrokerEventSink:
    """Fire-and-forget delivery of turn events to plugin subscribers."""

    client: httpx.AsyncClient
    broker_url: str

    async def post_event(self, kind: str, payload: dict[str, JsonValue]) -> None:
        try:
            await self.client.post(
                f"{self.broker_url.rstrip('/')}/plugin-broker/events",
                json={"kind": kind, "payload": payload},
                timeout=3.0,
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as error:
            logger.warning("plugin_event_post_failed", error=type(error).__name__)


def _fail(code: str, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message, retryable=retryable))
