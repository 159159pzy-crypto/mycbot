"""The authenticated operator console API."""

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, JsonValue

from mybot.plugins.broker import PluginBroker
from mybot.repositories.audit import AuditRepository
from mybot.repositories.operator_views import OperatorViews
from mybot.repositories.system_kv import SystemKvRepository
from mybot.services.proactive import OPTIN_KEY
from mybot.settings import Settings
from mybot.tools.approvals import APPROVALS_KEY

PERSONA_KEY = "agent.system_prompt"


class ProactiveUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled_conversations: list[str]


class StreamLengths(Protocol):
    async def stream_len(self, stream: str) -> int: ...


class PersonaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str


class ApprovalsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_ids: list[str]


@dataclass(slots=True)
class OperatorContext:
    views: OperatorViews
    audit: AuditRepository
    config: SystemKvRepository
    broker: PluginBroker
    streams: StreamLengths
    settings: Settings


def create_operator_router(context: OperatorContext) -> APIRouter:
    router = APIRouter(prefix="/operator", tags=["operator"])

    @router.get("/ping")
    async def ping() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    @router.get("/conversations")
    async def conversations(  # pyright: ignore[reportUnusedFunction]
        limit: int = 50,
    ) -> dict[str, JsonValue]:
        rows = await context.views.conversations(limit=_bound(limit))
        return {"conversations": cast(JsonValue, rows)}

    @router.get("/conversations/{conversation_id}/messages")
    async def messages(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID, limit: int = 100
    ) -> dict[str, JsonValue]:
        rows = await context.views.messages(conversation_id, limit=_bound(limit))
        return {"messages": cast(JsonValue, rows)}

    @router.get("/conversations/{conversation_id}/turns")
    async def turns(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID, limit: int = 50
    ) -> dict[str, JsonValue]:
        rows = await context.views.turns(conversation_id, limit=_bound(limit))
        return {"turns": cast(JsonValue, rows)}

    @router.get("/memories")
    async def memories(  # pyright: ignore[reportUnusedFunction]
        scope: str | None = None, include_revoked: bool = False, limit: int = 100
    ) -> dict[str, JsonValue]:
        rows = await context.views.memories(
            scope=scope, include_revoked=include_revoked, limit=_bound(limit)
        )
        return {"memories": cast(JsonValue, rows)}

    @router.post("/memories/{memory_id}/revoke")
    async def revoke_memory(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
    ) -> dict[str, JsonValue]:
        revoked = await context.views.revoke_memory(memory_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="memory not found or already revoked")
        await context.audit.record("memory.revoke", {"memory_id": str(memory_id)})
        return {"revoked": True}

    @router.get("/plugins")
    async def plugins() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return context.broker.health()

    @router.get("/usage")
    async def usage(  # pyright: ignore[reportUnusedFunction]
        days: int = 14,
    ) -> dict[str, JsonValue]:
        rows = await context.views.usage(days=max(1, min(days, 90)))
        return {"usage": cast(JsonValue, rows)}

    @router.get("/metrics")
    async def metrics() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        settings = context.settings
        queues: dict[str, JsonValue] = {
            "ingest": await context.streams.stream_len(settings.ingest_stream),
            "outbound": await context.streams.stream_len(settings.outbound_stream),
            "ingest_dead_letter": await context.streams.stream_len(
                f"{settings.ingest_stream}:dead"
            ),
            "outbound_dead_letter": await context.streams.stream_len(
                f"{settings.outbound_stream}:dead"
            ),
        }
        return {
            "turns": cast(JsonValue, await context.views.turn_metrics()),
            "queues": cast(JsonValue, queues),
        }

    @router.get("/config/persona")
    async def get_persona() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        override = await context.config.get(PERSONA_KEY)
        return {
            "override": override if isinstance(override, str) else None,
            "default": context.settings.agent_system_prompt,
        }

    @router.put("/config/persona")
    async def set_persona(  # pyright: ignore[reportUnusedFunction]
        update: PersonaUpdate,
    ) -> dict[str, JsonValue]:
        text = update.system_prompt.strip()
        await context.config.set(PERSONA_KEY, text)
        await context.audit.record("persona.update", {"length": len(text)})
        return {"saved": True}

    @router.get("/config/approvals")
    async def get_approvals() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        value = await context.config.get(APPROVALS_KEY)
        approved: list[str] = (
            [str(item) for item in cast(list[object], value)]
            if isinstance(value, list)
            else []
        )
        return {"approved_ids": cast(JsonValue, approved)}

    @router.put("/config/approvals")
    async def set_approvals(  # pyright: ignore[reportUnusedFunction]
        update: ApprovalsUpdate,
    ) -> dict[str, JsonValue]:
        approved = sorted({tool_id.strip() for tool_id in update.approved_ids if tool_id.strip()})
        await context.config.set(APPROVALS_KEY, cast(JsonValue, approved))
        await context.audit.record(
            "approvals.update", {"approved_ids": cast(JsonValue, approved)}
        )
        return {"approved_ids": cast(JsonValue, approved)}

    @router.get("/config/proactive")
    async def get_proactive() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        value = await context.config.get(OPTIN_KEY)
        enabled: list[str] = (
            [str(item) for item in cast(list[object], value)]
            if isinstance(value, list)
            else []
        )
        return {
            "enabled_conversations": cast(JsonValue, enabled),
            "globally_enabled": context.settings.proactive_enabled,
        }

    @router.put("/config/proactive")
    async def set_proactive(  # pyright: ignore[reportUnusedFunction]
        update: ProactiveUpdate,
    ) -> dict[str, JsonValue]:
        enabled = sorted(
            {key.strip() for key in update.enabled_conversations if key.strip()}
        )
        await context.config.set(OPTIN_KEY, cast(JsonValue, enabled))
        await context.audit.record(
            "proactive.update", {"enabled_conversations": cast(JsonValue, enabled)}
        )
        return {"enabled_conversations": cast(JsonValue, enabled)}

    @router.get("/audit")
    async def audit(  # pyright: ignore[reportUnusedFunction]
        limit: int = 50,
    ) -> dict[str, JsonValue]:
        entries = await context.audit.recent(limit=_bound(limit))
        return {"entries": cast(JsonValue, entries)}

    return router


def _bound(limit: int) -> int:
    return max(1, min(limit, 500))
