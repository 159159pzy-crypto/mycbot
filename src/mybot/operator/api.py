"""The authenticated operator console API."""

from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mybot.contracts import (
    AgentProfile,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    ProfileMemoryPolicy,
    ReplyWillingnessPolicy,
)
from mybot.infrastructure.llm import ChatMessage, LlmClient, LlmError
from mybot.infrastructure.model_routing import (
    MODEL_CHANNELS_KEY,
    ModelAttemptSink,
    ModelCallAttempt,
    ModelChannel,
    ModelPurpose,
    calculate_cost_micros,
    legacy_model_channels,
)
from mybot.plugins.broker import PluginBroker
from mybot.repositories.audit import AuditRepository
from mybot.repositories.memory import MemoryRepository
from mybot.repositories.operator_views import OperatorViews
from mybot.repositories.participation import WillingnessAuditRepository
from mybot.repositories.profiles import ProfileRepository, ResolvedProfile
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


class ModelsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[ModelChannel]


class SandboxMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    text: str | None = None
    image_urls: list[str] = Field(default_factory=list)


class CoreBlockUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_identity_id: str | None = None
    content: str = ""
    token_budget: int = Field(default=400, ge=50, le=20_000)


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    system_prompt: str
    model_tier: str = "default"
    tool_capabilities: list[str] = Field(default_factory=list)
    memory: ProfileMemoryPolicy = Field(default_factory=ProfileMemoryPolicy)
    willingness: ReplyWillingnessPolicy = Field(default_factory=ReplyWillingnessPolicy)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    model_tier: str = "default"
    tool_capabilities: list[str] = Field(default_factory=list)
    memory: ProfileMemoryPolicy = Field(default_factory=ProfileMemoryPolicy)
    willingness: ReplyWillingnessPolicy = Field(default_factory=ReplyWillingnessPolicy)


class PersonaVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str
    change_note: str = ""


class PersonaRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version_id: UUID


class ProfileBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID | None = None


class RelationshipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impression: str
    familiarity: float = Field(ge=0.0, le=100.0)


class SandboxPublisher(Protocol):
    async def publish(self, payload: str) -> str: ...


@dataclass(slots=True)
class OperatorContext:
    views: OperatorViews
    audit: AuditRepository
    config: SystemKvRepository
    broker: PluginBroker
    streams: StreamLengths
    settings: Settings
    model_client: httpx.AsyncClient
    model_attempts: ModelAttemptSink
    sandbox: SandboxPublisher | None = None
    profiles: ProfileRepository | None = None
    memory: MemoryRepository | None = None
    willingness: WillingnessAuditRepository | None = None


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

    @router.get("/conversations/{conversation_id}/traces")
    async def traces(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID, trace_id: str | None = None, limit: int = 200
    ) -> dict[str, JsonValue]:
        rows = await context.views.traces(
            conversation_id, trace_id=trace_id, limit=_bound(limit)
        )
        return {"traces": cast(JsonValue, rows)}

    @router.post("/sandbox/messages")
    async def sandbox_message(  # pyright: ignore[reportUnusedFunction]
        request: SandboxMessageInput,
    ) -> dict[str, JsonValue]:
        if context.sandbox is None:
            raise HTTPException(status_code=503, detail="sandbox stream is unavailable")
        text = (request.text or "").strip()
        image_urls = [_validated_image_url(value) for value in request.image_urls[:4]]
        if not text and not image_urls:
            raise HTTPException(status_code=422, detail="text or image_urls is required")
        if len(text) > context.settings.input_max_chars:
            raise HTTPException(status_code=422, detail="sandbox text is too long")
        session_id = _sandbox_session_id(request.session_id)
        from mybot.adapters import InboundEvent
        from mybot.contracts import ChatKind, ImageSegment, MessageEnvelope, Platform, TextSegment

        segments: list[TextSegment | ImageSegment] = []
        if text:
            segments.append(TextSegment(text=text))
        segments.extend(ImageSegment(url=url) for url in image_urls)
        envelope = MessageEnvelope(
            id=f"sandbox:{context.settings.sandbox_connection_id}:{uuid4()}",
            connection_id=context.settings.sandbox_connection_id,
            platform=Platform.SANDBOX,
            chat_kind=ChatKind.DIRECT,
            chat_id=session_id,
            sender_identity_id="sandbox:operator",
            occurred_at=datetime.now(tz=UTC),
            segments=tuple(segments),
            ephemeral=True,
        )
        await context.sandbox.publish(InboundEvent(envelope=envelope).model_dump_json())
        await context.audit.record(
            "sandbox.message",
            {
                "session_id": session_id,
                "trace_id": envelope.trace_id,
                "has_text": bool(text),
                "image_count": len(image_urls),
            },
        )
        return {
            "accepted": True,
            "session_id": session_id,
            "trace_id": envelope.trace_id,
            "envelope_id": envelope.id,
        }

    @router.get("/sandbox/{session_id}")
    async def sandbox_session(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
    ) -> dict[str, JsonValue]:
        from mybot.contracts import ChatKind, ConversationKey

        normalized = _sandbox_session_id(session_id)
        key = ConversationKey(
            connection_id=context.settings.sandbox_connection_id,
            chat_kind=ChatKind.DIRECT,
            chat_id=normalized,
        )
        conversation = await context.views.conversation_by_stable_key(key.stable_key)
        if conversation is None:
            return {"status": "pending", "session_id": normalized}
        conversation_id = UUID(str(conversation["id"]))
        message_rows = await context.views.messages(conversation_id, limit=200)
        turn_rows = await context.views.turns(conversation_id, limit=100)
        trace_rows = await context.views.traces(conversation_id, limit=500)
        return {
            "status": "ready",
            "session_id": normalized,
            "conversation": cast(JsonValue, conversation),
            "messages": cast(JsonValue, message_rows),
            "turns": cast(JsonValue, turn_rows),
            "traces": cast(JsonValue, trace_rows),
        }

    @router.get("/memories")
    async def memories(  # pyright: ignore[reportUnusedFunction]
        scope: str | None = None,
        include_revoked: bool = False,
        state: Literal["active", "invalidated", "revoked", "all"] = "active",
        limit: int = 100,
    ) -> dict[str, JsonValue]:
        rows = await context.views.memories(
            scope=scope,
            include_revoked=include_revoked,
            state=state,
            limit=_bound(limit),
        )
        return {"memories": cast(JsonValue, rows)}

    @router.get("/memories/{memory_id}/history")
    async def memory_history(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID,
    ) -> dict[str, JsonValue]:
        rows = await context.views.memory_history(memory_id)
        if not rows:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"history": cast(JsonValue, rows)}

    @router.get("/memories/{memory_id}/operations")
    async def memory_operations(  # pyright: ignore[reportUnusedFunction]
        memory_id: UUID, limit: int = 100
    ) -> dict[str, JsonValue]:
        rows = await context.views.memory_operations(memory_id, limit=_bound(limit))
        return {"operations": cast(JsonValue, rows)}

    @router.get("/core-blocks")
    async def core_blocks() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        rows = await context.views.core_blocks()
        return {"core_blocks": cast(JsonValue, rows)}

    @router.put("/core-blocks/{label}")
    async def replace_core_block(  # pyright: ignore[reportUnusedFunction]
        label: str, update: CoreBlockUpdate
    ) -> dict[str, JsonValue]:
        try:
            block = await context.views.replace_core_block(
                label=label,
                subject_identity_id=update.subject_identity_id,
                content=update.content,
                token_budget=update.token_budget,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record(
            "core_block.replace",
            {
                "label": label,
                "subject_identity_id": update.subject_identity_id,
                "content_length": len(update.content),
                "token_budget": update.token_budget,
            },
        )
        return {"core_block": cast(JsonValue, block)}

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

    @router.get("/models")
    async def models() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        raw = await context.config.get(MODEL_CHANNELS_KEY)
        if isinstance(raw, list):
            channels = [ModelChannel.model_validate(item) for item in raw]
            source = "runtime"
        else:
            channels = list(legacy_model_channels(context.settings))
            source = "legacy"
        usage = await context.views.model_channel_usage(days=30)
        daily_usage = await context.views.model_daily_usage(days=30)
        conversation_usage = await context.views.model_conversation_usage(days=30, limit=10)
        return {
            "source": source,
            "channels": cast(
                JsonValue,
                [channel.model_dump(mode="json") for channel in channels],
            ),
            "usage": cast(JsonValue, usage),
            "daily_usage": cast(JsonValue, daily_usage),
            "conversation_usage": cast(JsonValue, conversation_usage),
        }

    @router.put("/models")
    async def set_models(  # pyright: ignore[reportUnusedFunction]
        update: ModelsUpdate,
    ) -> dict[str, JsonValue]:
        names = [channel.name for channel in update.channels]
        if len(names) != len(set(names)):
            raise HTTPException(status_code=422, detail="model channel names must be unique")
        embedding_models = {
            channel.model_map[ModelPurpose.EMBEDDING].model
            for channel in update.channels
            if channel.enabled and ModelPurpose.EMBEDDING in channel.model_map
        }
        if len(embedding_models) > 1:
            raise HTTPException(
                status_code=422,
                detail="all enabled embedding channels must use the same embedding model",
            )
        serialized = [channel.model_dump(mode="json") for channel in update.channels]
        await context.config.set(MODEL_CHANNELS_KEY, cast(JsonValue, serialized))
        await context.audit.record(
            "models.update",
            {"channels": cast(JsonValue, sorted(names))},
        )
        return {"saved": True, "channels": cast(JsonValue, serialized)}

    @router.post("/models/{channel_name}/test")
    async def test_model(  # pyright: ignore[reportUnusedFunction]
        channel_name: str,
    ) -> dict[str, JsonValue]:
        raw = await context.config.get(MODEL_CHANNELS_KEY)
        channels = (
            [ModelChannel.model_validate(item) for item in raw]
            if isinstance(raw, list)
            else list(legacy_model_channels(context.settings))
        )
        channel = next((item for item in channels if item.name == channel_name), None)
        if channel is None:
            raise HTTPException(status_code=404, detail="model channel not found")
        target = channel.model_map.get(ModelPurpose.CHAT)
        if target is None:
            raise HTTPException(status_code=409, detail="channel has no chat model")
        api_key = _model_secret(context.settings, channel.api_key_env)
        started = monotonic()
        client = LlmClient(
            client=context.model_client,
            base_url=channel.base_url,
            api_key=api_key,
            model=target.model,
            temperature=0.0,
            max_output_tokens=8,
            timeout_seconds=min(context.settings.llm_timeout_seconds, 20.0),
            max_retries=0,
        )
        try:
            reply = await client.complete(
                [ChatMessage(role="user", content="Reply with exactly OK")]
            )
        except Exception as error:
            latency_ms = max(0, int((monotonic() - started) * 1_000))
            retryable = isinstance(error, LlmError) and error.retryable
            await context.model_attempts.record(
                ModelCallAttempt(
                    purpose=ModelPurpose.CHAT,
                    channel=channel.name,
                    model=target.model,
                    status="retryable_error" if retryable else "permanent_error",
                    latency_ms=latency_ms,
                    input_price_per_million=target.input_price_per_million,
                    output_price_per_million=target.output_price_per_million,
                    error_code=type(error).__name__,
                )
            )
            await context.audit.record(
                "models.test",
                {
                    "channel": channel.name,
                    "ok": False,
                    "error_code": type(error).__name__,
                },
            )
            raise HTTPException(
                status_code=502,
                detail=f"model connectivity test failed: {type(error).__name__}",
            ) from error
        latency_ms = max(0, int((monotonic() - started) * 1_000))
        await context.model_attempts.record(
            ModelCallAttempt(
                purpose=ModelPurpose.CHAT,
                channel=channel.name,
                model=reply.model,
                status="success",
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                latency_ms=latency_ms,
                input_price_per_million=target.input_price_per_million,
                output_price_per_million=target.output_price_per_million,
                cost_usd_micros=calculate_cost_micros(
                    target, reply.prompt_tokens, reply.completion_tokens
                ),
            )
        )
        await context.audit.record("models.test", {"channel": channel.name, "ok": True})
        return {
            "ok": True,
            "channel": channel.name,
            "model": reply.model,
            "latency_ms": latency_ms,
        }

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
        participation = (
            await context.willingness.metrics(hours=24)
            if context.willingness is not None
            else {"allowed": 0, "blocked": 0}
        )
        return {
            "turns": cast(JsonValue, await context.views.turn_metrics()),
            "queues": cast(JsonValue, queues),
            "willingness": cast(JsonValue, participation),
        }

    @router.get("/profiles")
    async def profiles() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        repository = _profiles(context)
        rows = await repository.list_all()
        if not rows:
            rows = (
                await repository.ensure_default(
                    system_prompt=context.settings.agent_system_prompt,
                    tool_capabilities=context.settings.granted_capabilities(),
                ),
            )
        bindings = await repository.bindings()
        return {
            "profiles": cast(JsonValue, [_profile_json(row) for row in rows]),
            "bindings": cast(
                JsonValue,
                [
                    {"conversation_id": str(conversation_id), "profile_id": str(profile_id)}
                    for conversation_id, profile_id in bindings
                ],
            ),
        }

    @router.post("/profiles")
    async def create_profile(  # pyright: ignore[reportUnusedFunction]
        update: ProfileCreate,
    ) -> dict[str, JsonValue]:
        repository = _profiles(context)
        name = update.name.strip()
        prompt = update.system_prompt.strip()
        if not name or not prompt:
            raise HTTPException(status_code=422, detail="name and system_prompt are required")
        try:
            created = await repository.create(
                AgentProfile(
                    name=name,
                    description=update.description.strip(),
                    model_tier=update.model_tier.strip() or "default",
                    tool_capabilities=tuple(
                        dict.fromkeys(
                            capability.strip()
                            for capability in update.tool_capabilities
                            if capability.strip()
                        )
                    ),
                    memory=update.memory,
                    willingness=update.willingness,
                ),
                system_prompt=prompt,
                change_note="created in operator console",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record("profile.create", {"profile_id": str(created.profile.id)})
        return {"profile": cast(JsonValue, _profile_json(created))}

    @router.put("/profiles/{profile_id}")
    async def update_profile(  # pyright: ignore[reportUnusedFunction]
        profile_id: UUID, update: ProfileUpdate
    ) -> dict[str, JsonValue]:
        repository = _profiles(context)
        current = await repository.get(profile_id)
        if current is None:
            raise HTTPException(status_code=404, detail="profile not found")
        try:
            saved = await repository.update(
                AgentProfile(
                    id=profile_id,
                    name=update.name.strip(),
                    description=update.description.strip(),
                    active_persona_version_id=current.persona.id,
                    model_tier=update.model_tier.strip() or "default",
                    tool_capabilities=tuple(
                        dict.fromkeys(
                            capability.strip()
                            for capability in update.tool_capabilities
                            if capability.strip()
                        )
                    ),
                    memory=update.memory,
                    willingness=update.willingness,
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record("profile.update", {"profile_id": str(profile_id)})
        return {"profile": cast(JsonValue, _profile_json(saved))}

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(  # pyright: ignore[reportUnusedFunction]
        profile_id: UUID,
    ) -> dict[str, JsonValue]:
        try:
            deleted = await _profiles(context).delete(profile_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="profile not found")
        await context.audit.record("profile.delete", {"profile_id": str(profile_id)})
        return {"deleted": True}

    @router.get("/profiles/{profile_id}/personas")
    async def persona_versions(  # pyright: ignore[reportUnusedFunction]
        profile_id: UUID,
    ) -> dict[str, JsonValue]:
        versions = await _profiles(context).persona_versions(profile_id)
        return {
            "versions": cast(
                JsonValue,
                [version.model_dump(mode="json") for version in versions],
            )
        }

    @router.post("/profiles/{profile_id}/personas")
    async def create_persona_version(  # pyright: ignore[reportUnusedFunction]
        profile_id: UUID, update: PersonaVersionCreate
    ) -> dict[str, JsonValue]:
        try:
            version = await _profiles(context).create_persona_version(
                profile_id,
                system_prompt=update.system_prompt,
                change_note=update.change_note,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record(
            "persona.version.create",
            {"profile_id": str(profile_id), "persona_version_id": str(version.id)},
        )
        return {"version": cast(JsonValue, version.model_dump(mode="json"))}

    @router.post("/profiles/{profile_id}/personas/rollback")
    async def rollback_persona(  # pyright: ignore[reportUnusedFunction]
        profile_id: UUID, update: PersonaRollback
    ) -> dict[str, JsonValue]:
        try:
            version = await _profiles(context).rollback_persona(
                profile_id, target_version_id=update.target_version_id
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        await context.audit.record(
            "persona.version.rollback",
            {
                "profile_id": str(profile_id),
                "target_version_id": str(update.target_version_id),
                "new_version_id": str(version.id),
            },
        )
        return {"version": cast(JsonValue, version.model_dump(mode="json"))}

    @router.put("/conversations/{conversation_id}/profile")
    async def bind_profile(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID, update: ProfileBindingUpdate
    ) -> dict[str, JsonValue]:
        repository = _profiles(context)
        if update.profile_id is None:
            await repository.unbind(conversation_id)
        else:
            if await repository.get(update.profile_id) is None:
                raise HTTPException(status_code=404, detail="profile not found")
            await repository.bind(conversation_id, update.profile_id)
        await context.audit.record(
            "profile.bind",
            {
                "conversation_id": str(conversation_id),
                "profile_id": str(update.profile_id) if update.profile_id else None,
            },
        )
        return {"saved": True}

    @router.get("/relationships")
    async def relationships(  # pyright: ignore[reportUnusedFunction]
        limit: int = 200,
    ) -> dict[str, JsonValue]:
        return {
            "relationships": cast(
                JsonValue, await context.views.relationships(limit=_bound(limit))
            )
        }

    @router.put("/relationships/{subject_identity_id}")
    async def update_relationship(  # pyright: ignore[reportUnusedFunction]
        subject_identity_id: str, update: RelationshipUpdate
    ) -> dict[str, JsonValue]:
        if context.memory is None:
            raise HTTPException(status_code=503, detail="relationship store is unavailable")
        subject = subject_identity_id.strip()
        impression = update.impression.strip()
        if not subject or not impression:
            raise HTTPException(status_code=422, detail="subject and impression are required")
        item = MemoryItem(
            scope=MemoryScope.SUBJECT,
            subject_identity_id=subject,
            kind="RELATIONSHIP",
            content=impression,
            source_message_ids=(f"operator:{uuid4()}",),
            confidence=1.0,
            relationship_score=update.familiarity,
            privacy=MemoryPrivacy.PRIVATE,
        )
        applied = await context.memory.upsert_relationship(
            item,
            source="operator_relationship_edit",
            now=datetime.now(tz=UTC),
        )
        await context.audit.record(
            "relationship.update",
            {
                "subject_identity_id": subject,
                "memory_id": str(applied.memory_id),
                "familiarity": update.familiarity,
            },
        )
        return {"saved": True, "memory_id": str(applied.memory_id)}

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
            [str(item) for item in cast(list[object], value)] if isinstance(value, list) else []
        )
        return {"approved_ids": cast(JsonValue, approved)}

    @router.put("/config/approvals")
    async def set_approvals(  # pyright: ignore[reportUnusedFunction]
        update: ApprovalsUpdate,
    ) -> dict[str, JsonValue]:
        approved = sorted({tool_id.strip() for tool_id in update.approved_ids if tool_id.strip()})
        await context.config.set(APPROVALS_KEY, cast(JsonValue, approved))
        await context.audit.record("approvals.update", {"approved_ids": cast(JsonValue, approved)})
        return {"approved_ids": cast(JsonValue, approved)}

    @router.get("/config/proactive")
    async def get_proactive() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        value = await context.config.get(OPTIN_KEY)
        enabled: list[str] = (
            [str(item) for item in cast(list[object], value)] if isinstance(value, list) else []
        )
        return {
            "enabled_conversations": cast(JsonValue, enabled),
            "globally_enabled": context.settings.proactive_enabled,
        }

    @router.put("/config/proactive")
    async def set_proactive(  # pyright: ignore[reportUnusedFunction]
        update: ProactiveUpdate,
    ) -> dict[str, JsonValue]:
        enabled = sorted({key.strip() for key in update.enabled_conversations if key.strip()})
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


def _profiles(context: OperatorContext) -> ProfileRepository:
    if context.profiles is None:
        raise HTTPException(status_code=503, detail="profile store is unavailable")
    return context.profiles


def _profile_json(resolved: ResolvedProfile) -> dict[str, JsonValue]:
    return {
        "profile": cast(JsonValue, resolved.profile.model_dump(mode="json")),
        "persona": cast(JsonValue, resolved.persona.model_dump(mode="json")),
        "created_at": resolved.created_at.isoformat(),
        "updated_at": resolved.updated_at.isoformat(),
    }


def _sandbox_session_id(value: str | None) -> str:
    candidate = (value or str(uuid4())).strip()
    if not candidate or len(candidate) > 80:
        raise HTTPException(status_code=422, detail="invalid sandbox session id")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if any(char not in allowed for char in candidate):
        raise HTTPException(status_code=422, detail="invalid sandbox session id")
    return candidate


def _validated_image_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="sandbox image URL must use http or https",
        )
    if len(candidate) > 2_000:
        raise HTTPException(status_code=422, detail="sandbox image URL is too long")
    return candidate


def _model_secret(settings: Settings, name: str | None) -> str | None:
    if name is None:
        return None
    return settings.model_secret(name)
