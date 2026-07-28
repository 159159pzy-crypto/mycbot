"""The authenticated operator console API."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mybot.contracts import (
    AgentProfile,
    AnnotationReply,
    KnowledgeDocument,
    KnowledgeIngestTask,
    KnowledgeScope,
    KnowledgeSourceType,
    MemoryItem,
    MemoryPrivacy,
    MemoryScope,
    PluginManifest,
    ProfileMemoryPolicy,
    ReplyWillingnessPolicy,
)
from mybot.contracts.json import thaw_json_object
from mybot.engine.knowledge import KnowledgeSearchService
from mybot.infrastructure.llm import ChatMessage, LlmClient, LlmError
from mybot.infrastructure.model_routing import (
    MODEL_CHANNELS_KEY,
    EmbeddingBatch,
    ModelAttemptSink,
    ModelCallAttempt,
    ModelChannel,
    ModelPurpose,
    calculate_cost_micros,
    legacy_model_channels,
)
from mybot.plugins.broker import PluginBroker
from mybot.plugins.config import PluginConfigError, validate_plugin_config
from mybot.plugins.control import PluginControlStore
from mybot.plugins.tooling import PluginInstaller
from mybot.repositories.audit import AuditRepository
from mybot.repositories.knowledge import KnowledgeRepository
from mybot.repositories.memory import MemoryRepository
from mybot.repositories.operator_views import OperatorViews
from mybot.repositories.pairing import PairingRepository
from mybot.repositories.participation import WillingnessAuditRepository
from mybot.repositories.profiles import ProfileRepository, ResolvedProfile
from mybot.repositories.system_kv import SystemKvRepository
from mybot.services.proactive import OPTIN_KEY
from mybot.settings import Settings
from mybot.skills import SkillDocument, SkillStore
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


class SkillContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class EnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class PluginActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["enable", "disable", "reload"]


class PluginConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, JsonValue]


class PluginInstallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class PairingPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    policy: Literal["open", "paired", "allowlist"]
    allowlist: list[str] = Field(default_factory=list)


class PairingGenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["qq", "telegram"]
    connection_id: str
    subject_identity_id: str


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    scope: Literal["ALL", "GLOBAL", "CONVERSATION"] = "ALL"
    conversation_stable_key: str | None = None


class AnnotationCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: KnowledgeScope = KnowledgeScope.GLOBAL
    conversation_id: UUID | None = None
    question: str
    answer: str
    threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    enabled: bool = True


class AnnotationFromMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str | None = None
    threshold: float = Field(default=0.92, ge=0.0, le=1.0)


class SandboxPublisher(Protocol):
    async def publish(self, payload: str) -> str: ...


class Embeddings(Protocol):
    async def embed_with_model(self, texts: Sequence[str]) -> EmbeddingBatch: ...


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
    knowledge: KnowledgeRepository | None = None
    knowledge_tasks: SandboxPublisher | None = None
    embeddings: Embeddings | None = None
    skills: SkillStore | None = None
    plugin_control: PluginControlStore | None = None
    plugin_installer: PluginInstaller | None = None
    pairing: PairingRepository | None = None


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

    @router.get("/knowledge/documents")
    async def knowledge_documents() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        repository = _knowledge(context)
        return {"documents": cast(JsonValue, await repository.list_documents())}

    @router.post("/knowledge/documents")
    async def upload_knowledge_document(  # pyright: ignore[reportUnusedFunction]
        file: Annotated[UploadFile, File()],
        scope: Annotated[KnowledgeScope, Form()] = KnowledgeScope.GLOBAL,
        conversation_id: Annotated[UUID | None, Form()] = None,
        title: Annotated[str | None, Form()] = None,
    ) -> dict[str, JsonValue]:
        repository = _knowledge(context)
        if context.knowledge_tasks is None:
            raise HTTPException(status_code=503, detail="knowledge stream is unavailable")
        filename = Path(file.filename or "document").name
        suffix = Path(filename).suffix.lower()
        source_types = {
            ".md": KnowledgeSourceType.MARKDOWN,
            ".markdown": KnowledgeSourceType.MARKDOWN,
            ".txt": KnowledgeSourceType.TEXT,
            ".pdf": KnowledgeSourceType.PDF,
        }
        source_type = source_types.get(suffix)
        if source_type is None:
            raise HTTPException(status_code=415, detail="only Markdown, TXT, and PDF are supported")
        content = await file.read(context.settings.knowledge_max_upload_bytes + 1)
        if not content:
            raise HTTPException(status_code=422, detail="document is empty")
        if len(content) > context.settings.knowledge_max_upload_bytes:
            raise HTTPException(status_code=413, detail="document exceeds upload size limit")
        try:
            document = KnowledgeDocument(
                title=(title or Path(filename).stem).strip(),
                source_type=source_type,
                scope=scope,
                conversation_id=conversation_id,
                original_filename=filename,
                content_hash=sha256(content).hexdigest(),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        stored, created = await repository.create_document(document, content=content)
        published = await _flush_knowledge_outbox(context, document_id=stored.id)
        await context.audit.record(
            "knowledge.document.upload",
            {"document_id": str(stored.id), "created": created, "scope": stored.scope.value},
        )
        return {
            "created": created,
            "published": published,
            "document": cast(JsonValue, stored.model_dump(mode="json")),
        }

    @router.delete("/knowledge/documents/{document_id}")
    async def delete_knowledge_document(  # pyright: ignore[reportUnusedFunction]
        document_id: UUID,
    ) -> dict[str, JsonValue]:
        deleted = await _knowledge(context).delete_document(document_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="knowledge document not found")
        await context.audit.record("knowledge.document.delete", {"document_id": str(document_id)})
        return {"deleted": True}

    @router.post("/knowledge/documents/{document_id}/reingest")
    async def reingest_knowledge_document(  # pyright: ignore[reportUnusedFunction]
        document_id: UUID,
    ) -> dict[str, JsonValue]:
        if context.knowledge_tasks is None:
            raise HTTPException(status_code=503, detail="knowledge stream is unavailable")
        document = await _knowledge(context).requeue_document(document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="knowledge document not found")
        published = await _flush_knowledge_outbox(context, document_id=document.id)
        return {
            "queued": True,
            "published": published,
            "generation": document.generation,
        }

    @router.post("/knowledge/search")
    async def test_knowledge_search(  # pyright: ignore[reportUnusedFunction]
        request: KnowledgeSearchInput,
    ) -> dict[str, JsonValue]:
        embeddings = _embeddings(context)
        query = request.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="query is required")
        if request.scope != "GLOBAL" and not request.conversation_stable_key:
            if request.scope == "CONVERSATION":
                raise HTTPException(
                    status_code=422,
                    detail="conversation_stable_key is required for conversation scope",
                )
        hits = await KnowledgeSearchService(
            store=_knowledge(context),
            embeddings=embeddings,
        ).search(
            query,
            conversation_stable_key=request.conversation_stable_key,
            allow_conversation=request.scope != "GLOBAL" and bool(request.conversation_stable_key),
            include_global=request.scope != "CONVERSATION",
            top_k=request.top_k,
            threshold=request.threshold,
        )
        await context.audit.record(
            "knowledge.search.test",
            {"top_k": request.top_k, "threshold": request.threshold, "scope": request.scope},
        )
        return {
            "query": query,
            "results": cast(JsonValue, [hit.model_dump(mode="json") for hit in hits]),
        }

    @router.get("/knowledge/annotations")
    async def annotations() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        return {"annotations": cast(JsonValue, await _knowledge(context).list_annotations())}

    @router.post("/knowledge/annotations")
    async def create_annotation(  # pyright: ignore[reportUnusedFunction]
        request: AnnotationCreateInput,
    ) -> dict[str, JsonValue]:
        question = request.question.strip()
        answer = request.answer.strip()
        if not question or not answer:
            raise HTTPException(status_code=422, detail="question and answer are required")
        annotation = AnnotationReply(
            scope=request.scope,
            conversation_id=request.conversation_id,
            question=question,
            answer=answer,
            threshold=request.threshold,
            enabled=request.enabled,
        )
        batch = await _embeddings(context).embed_with_model([question])
        saved = await _knowledge(context).create_annotation(
            annotation,
            embedding=batch.vectors[0],
            embedding_model=batch.model,
        )
        await context.audit.record("annotation.create", {"annotation_id": str(saved.id)})
        return {"annotation": cast(JsonValue, saved.model_dump(mode="json"))}

    @router.post("/conversations/{conversation_id}/messages/{message_id}/annotation")
    async def annotation_from_message(  # pyright: ignore[reportUnusedFunction]
        conversation_id: UUID,
        message_id: UUID,
        request: AnnotationFromMessageInput,
    ) -> dict[str, JsonValue]:
        repository = _knowledge(context)
        # The repository resolves the previous inbound question. Embed that exact
        # question by first using an explicit one, or querying it through a
        # lightweight placeholder and replacing it inside the transaction.
        if request.question is None or not request.question.strip():
            pair = await repository.message_annotation_pair(conversation_id, message_id)
            if pair is None:
                raise HTTPException(status_code=422, detail="no question/answer pair found")
            question, _answer = pair
        else:
            question = request.question.strip()
        batch = await _embeddings(context).embed_with_model([question])
        saved = await repository.annotation_from_message(
            conversation_id=conversation_id,
            message_id=message_id,
            question=question,
            threshold=request.threshold,
            embedding=batch.vectors[0],
            embedding_model=batch.model,
        )
        if saved is None:
            raise HTTPException(status_code=422, detail="no question/answer pair found")
        await context.audit.record("annotation.from_message", {"annotation_id": str(saved.id)})
        return {"annotation": cast(JsonValue, saved.model_dump(mode="json"))}

    @router.delete("/knowledge/annotations/{annotation_id}")
    async def delete_annotation(  # pyright: ignore[reportUnusedFunction]
        annotation_id: UUID,
    ) -> dict[str, JsonValue]:
        if not await _knowledge(context).delete_annotation(annotation_id):
            raise HTTPException(status_code=404, detail="annotation not found")
        await context.audit.record("annotation.delete", {"annotation_id": str(annotation_id)})
        return {"deleted": True}

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

    @router.get("/skills")
    async def skills() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        if context.skills is None:
            return {"skills": []}
        return {
            "skills": cast(
                JsonValue,
                [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "trigger": skill.trigger,
                        "content": skill.content,
                        "enabled": skill.enabled,
                    }
                    for skill in context.skills.list()
                ],
            )
        }

    @router.put("/skills/{name}")
    async def save_skill(  # pyright: ignore[reportUnusedFunction]
        name: str, update: SkillContentUpdate
    ) -> dict[str, JsonValue]:
        if context.skills is None:
            raise HTTPException(status_code=503, detail="skills are unavailable")
        try:
            skill = context.skills.save(name, update.content)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record("skill.save", {"name": name})
        return {"skill": cast(JsonValue, _skill_view(skill))}

    @router.put("/skills/{name}/enabled")
    async def set_skill_enabled(  # pyright: ignore[reportUnusedFunction]
        name: str, update: EnabledUpdate
    ) -> dict[str, JsonValue]:
        if context.skills is None:
            raise HTTPException(status_code=503, detail="skills are unavailable")
        try:
            skill = context.skills.set_enabled(name, update.enabled)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="skill not found") from error
        await context.audit.record(
            "skill.enable" if update.enabled else "skill.disable", {"name": name}
        )
        return {"skill": cast(JsonValue, _skill_view(skill))}

    @router.get("/plugins")
    async def plugins() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        health = context.broker.health()
        status_document = context.plugin_control.status() if context.plugin_control else {}
        statuses = status_document.get("plugins")
        status_map: dict[str, JsonValue] = statuses if isinstance(statuses, dict) else {}
        raw_plugins = health.get("plugins")
        seen: set[str] = set()
        if isinstance(raw_plugins, list):
            for raw in raw_plugins:
                if not isinstance(raw, dict):
                    continue
                plugin_id = raw.get("id")
                if isinstance(plugin_id, str):
                    seen.add(plugin_id)
                status = status_map.get(plugin_id) if isinstance(plugin_id, str) else None
                if isinstance(status, dict):
                    raw.update(status)
                if context.plugin_control is not None and isinstance(plugin_id, str):
                    raw["config"] = context.plugin_control.control(plugin_id).config or {}
            for plugin_id, status in status_map.items():
                if (
                    plugin_id in seen
                    or not isinstance(status, dict)
                ):
                    continue
                manifest = _plugin_manifest(context, plugin_id)
                if manifest is None:
                    continue
                raw_plugins.append(
                    {
                        "id": manifest.id,
                        "version": manifest.version,
                        "runner_id": status.get("runner_id", ""),
                        "tools": [tool.id for tool in manifest.tools],
                        "event_hooks": list(manifest.event_hooks),
                        "tasks": [task.model_dump(mode="json") for task in manifest.tasks],
                        "granted_capabilities": list(
                            context.settings.plugin_grants().get(manifest.id, ())
                        ),
                        "config_schema": thaw_json_object(manifest.config_schema),
                        "requires": thaw_json_object(manifest.requires),
                        "config": (
                            context.plugin_control.control(plugin_id).config
                            if context.plugin_control is not None
                            else {}
                        ),
                        **status,
                    }
                )
        health["supervision"] = cast(JsonValue, status_map)
        return health

    @router.post("/plugins/{plugin_id}/action")
    async def plugin_action(  # pyright: ignore[reportUnusedFunction]
        plugin_id: str, update: PluginActionInput
    ) -> dict[str, JsonValue]:
        if context.plugin_control is None:
            raise HTTPException(status_code=503, detail="plugin control is unavailable")
        control = context.plugin_control.request_action(plugin_id, update.action)
        await context.audit.record(
            f"plugin.{update.action}", {"plugin_id": plugin_id}
        )
        return {
            "plugin_id": plugin_id,
            "enabled": control.enabled,
            "generation": control.generation,
        }

    @router.put("/plugins/{plugin_id}/config")
    async def plugin_config(  # pyright: ignore[reportUnusedFunction]
        plugin_id: str, update: PluginConfigInput
    ) -> dict[str, JsonValue]:
        if context.plugin_control is None:
            raise HTTPException(status_code=503, detail="plugin control is unavailable")
        manifest = _plugin_manifest(context, plugin_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="plugin not found")
        try:
            validate_plugin_config(thaw_json_object(manifest.config_schema), update.config)
        except PluginConfigError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        context.plugin_control.set_config(plugin_id, update.config)
        delivered = context.broker.post_plugin_event(
            plugin_id,
            "plugin.config.changed",
            {"plugin_id": plugin_id, "config": cast(JsonValue, update.config)},
        )
        await context.audit.record(
            "plugin.config", {"plugin_id": plugin_id, "delivered": delivered}
        )
        return {"plugin_id": plugin_id, "config": cast(JsonValue, update.config)}

    @router.get("/plugins/registry")
    async def plugin_registry() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        if context.plugin_installer is None:
            return {"plugins": [], "managed": []}
        return {
            "plugins": cast(JsonValue, context.plugin_installer.registry()),
            "managed": (
                context.plugin_control.managed().get("plugins", [])
                if context.plugin_control is not None
                else []
            ),
        }

    @router.post("/plugins/install")
    async def install_plugin(  # pyright: ignore[reportUnusedFunction]
        update: PluginInstallInput,
    ) -> dict[str, JsonValue]:
        if context.plugin_installer is None:
            raise HTTPException(status_code=503, detail="plugin installer is unavailable")
        try:
            installed = await context.plugin_installer.install(update.name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="registry plugin not found") from error
        except (ValueError, httpx.HTTPError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await context.audit.record(
            "plugin.install",
            {"plugin_id": installed.manifest.id, "version": installed.manifest.version},
        )
        return {
            "plugin": cast(JsonValue, installed.manifest.model_dump(mode="json")),
            "install_dir": str(installed.install_dir),
        }

    @router.get("/pairing")
    async def pairing() -> dict[str, JsonValue]:  # pyright: ignore[reportUnusedFunction]
        repository = _pairing(context)
        policies: list[dict[str, JsonValue]] = []
        for platform, connection_id in (
            ("qq", context.settings.qq_connection_id),
            ("telegram", context.settings.telegram_connection_id),
        ):
            policy = await repository.policy(platform, connection_id)
            policies.append(
                {
                    "platform": platform,
                    "connection_id": connection_id,
                    "policy": policy.policy,
                    "allowlist": list(policy.allowlist),
                }
            )
        return {
            "policies": cast(JsonValue, policies),
            "requests": cast(JsonValue, await repository.pending()),
        }

    @router.put("/pairing/policies/{platform}")
    async def pairing_policy(  # pyright: ignore[reportUnusedFunction]
        platform: Literal["qq", "telegram"], update: PairingPolicyInput
    ) -> dict[str, JsonValue]:
        policy = await _pairing(context).set_policy(
            platform, update.connection_id, update.policy, update.allowlist
        )
        await context.audit.record(
            "pairing.policy",
            {"platform": platform, "connection_id": update.connection_id, "policy": update.policy},
        )
        return {
            "platform": platform,
            "connection_id": update.connection_id,
            "policy": policy.policy,
            "allowlist": cast(JsonValue, list(policy.allowlist)),
        }

    @router.post("/pairing/requests")
    async def generate_pairing(  # pyright: ignore[reportUnusedFunction]
        update: PairingGenerateInput,
    ) -> dict[str, JsonValue]:
        request, created = await _pairing(context).request(
            update.platform, update.connection_id, update.subject_identity_id
        )
        if request is None:
            raise HTTPException(status_code=429, detail="pending pairing request limit reached")
        await context.audit.record(
            "pairing.generate",
            {"platform": update.platform, "subject_identity_id": update.subject_identity_id},
        )
        return {
            "request": cast(
                JsonValue,
                {
                    "id": request.id,
                    "platform": request.platform,
                    "connection_id": request.connection_id,
                    "subject_identity_id": request.subject_identity_id,
                    "code": request.code,
                    "expires_at": request.expires_at,
                },
            ),
            "created": created,
        }

    @router.post("/pairing/requests/{request_id}/approve")
    async def approve_pairing(  # pyright: ignore[reportUnusedFunction]
        request_id: UUID,
    ) -> dict[str, JsonValue]:
        approved = await _pairing(context).approve(request_id)
        if not approved:
            raise HTTPException(status_code=404, detail="pairing request not found or expired")
        await context.audit.record("pairing.approve", {"request_id": str(request_id)})
        return {"approved": True}

    @router.post("/pairing/requests/{request_id}/dismiss")
    async def dismiss_pairing(  # pyright: ignore[reportUnusedFunction]
        request_id: UUID,
    ) -> dict[str, JsonValue]:
        dismissed = await _pairing(context).dismiss(request_id)
        if not dismissed:
            raise HTTPException(status_code=404, detail="pairing request not found")
        await context.audit.record("pairing.dismiss", {"request_id": str(request_id)})
        return {"dismissed": True}

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
            "knowledge": await context.streams.stream_len(settings.knowledge_stream),
            "ingest_dead_letter": await context.streams.stream_len(
                f"{settings.ingest_stream}:dead"
            ),
            "outbound_dead_letter": await context.streams.stream_len(
                f"{settings.outbound_stream}:dead"
            ),
            "knowledge_dead_letter": await context.streams.stream_len(
                f"{settings.knowledge_stream}:dead"
            ),
        }
        participation = (
            await context.willingness.metrics(hours=24)
            if context.willingness is not None
            else {"allowed": 0, "blocked": 0}
        )
        annotation_metrics = (
            await context.knowledge.annotation_metrics(hours=24)
            if context.knowledge is not None
            else {"hit": 0, "miss": 0, "error": 0, "total": 0, "hit_rate": 0.0}
        )
        return {
            "turns": cast(JsonValue, await context.views.turn_metrics()),
            "queues": cast(JsonValue, queues),
            "willingness": cast(JsonValue, participation),
            "annotations": cast(JsonValue, annotation_metrics),
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


def _knowledge(context: OperatorContext) -> KnowledgeRepository:
    if context.knowledge is None:
        raise HTTPException(status_code=503, detail="knowledge store is unavailable")
    return context.knowledge


def _embeddings(context: OperatorContext) -> Embeddings:
    if context.embeddings is None:
        raise HTTPException(status_code=503, detail="embedding service is unavailable")
    return context.embeddings


def _pairing(context: OperatorContext) -> PairingRepository:
    if context.pairing is None:
        raise HTTPException(status_code=503, detail="pairing store is unavailable")
    return context.pairing


def _skill_view(skill: SkillDocument) -> dict[str, JsonValue]:
    return {
        "name": skill.name,
        "description": skill.description,
        "trigger": skill.trigger,
        "content": skill.content,
        "enabled": skill.enabled,
    }


def _plugin_manifest(context: OperatorContext, plugin_id: str) -> PluginManifest | None:
    registered = context.broker.manifest(plugin_id)
    if registered is not None:
        return registered
    if context.plugin_control is None:
        return None
    from mybot.plugins.runner import load_plugin_entrypoint

    for source in context.plugin_control.sources(context.settings.plugin_config):
        try:
            plugin = load_plugin_entrypoint(source.entrypoint, source.python_path)
        except Exception:
            continue
        if plugin.manifest.id == plugin_id:
            return plugin.manifest
    return None


async def _flush_knowledge_outbox(
    context: OperatorContext, *, document_id: UUID
) -> bool:
    repository = _knowledge(context)
    publisher = context.knowledge_tasks
    if publisher is None:
        return False
    published = False
    for queued_id, generation in await repository.pending_ingest_tasks(
        document_id=document_id
    ):
        try:
            await publisher.publish(
                KnowledgeIngestTask(
                    document_id=queued_id, generation=generation
                ).model_dump_json()
            )
        except Exception as error:
            await repository.mark_ingest_task_failed(
                queued_id, generation, error_code=type(error).__name__
            )
            continue
        await repository.mark_ingest_task_published(queued_id, generation)
        published = True
    return published


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
