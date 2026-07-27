"""The agent-worker lifecycle: consume, persist, decide, and plan replies."""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import JsonValue

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.adapters.qq.translate import QQ_CAPABILITIES
from mybot.adapters.telegram.translate import TELEGRAM_CAPABILITIES
from mybot.contracts import (
    ConversationKey,
    MessageEnvelope,
    Platform,
    PlatformCapabilities,
    ReplyPlan,
    TurnAction,
    TurnDecision,
)
from mybot.engine.direct_replies import build_direct_reply
from mybot.engine.turn_policy import decide_turn, extract_command
from mybot.infrastructure.health import ReadinessResponse, create_readiness_service
from mybot.infrastructure.streams import (
    StreamConsumer,
    StreamPublisher,
    create_redis_backend,
)
from mybot.repositories.conversations import ConversationRecord
from mybot.repositories.messages import (
    StoredMessage,
    platform_message_id_from_envelope,
)
from mybot.repositories.traces import TraceStatus
from mybot.runtime import ProcessMode
from mybot.settings import Settings

logger = structlog.get_logger("mybot.agent_worker")

DEFAULT_CAPABILITIES: Mapping[Platform, PlatformCapabilities] = {
    Platform.QQ: QQ_CAPABILITIES,
    Platform.TELEGRAM: TELEGRAM_CAPABILITIES,
    Platform.SANDBOX: PlatformCapabilities(
        editing=False,
        replies=True,
        typing=False,
        proactive_messages=False,
        combined_media_text=True,
        threads=False,
        reactions=False,
        images=True,
        mentions=True,
        stickers=True,
        voice_messages=False,
    ),
}


class ConversationStore(Protocol):
    async def get_or_create(
        self, key: ConversationKey, *, platform: Platform, ephemeral: bool = False
    ) -> ConversationRecord: ...


class MessageStore(Protocol):
    async def record_inbound(
        self, conversation_id: UUID, envelope: MessageEnvelope
    ) -> StoredMessage: ...

    async def record_outbound(
        self,
        conversation_id: UUID,
        plan: ReplyPlan,
        *,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID: ...

    async def recent_outbound_platform_ids(
        self, conversation_id: UUID, *, limit: int = 50
    ) -> tuple[str, ...]: ...


class ReadinessSource(Protocol):
    async def check(self) -> ReadinessResponse: ...


class AgentEngine(Protocol):
    async def run_turn(
        self,
        *,
        conversation_id: UUID,
        stable_key: str,
        envelope: MessageEnvelope,
        decision: TurnDecision,
        inbound_message_id: UUID | None,
        capabilities: PlatformCapabilities,
    ) -> ReplyPlan: ...

    async def after_reply(
        self, *, envelope: MessageEnvelope, stable_key: str, reply_text: str
    ) -> None: ...


class MemoryCommands(Protocol):
    async def forget(
        self, *, subject_identity_id: str, conversation_stable_key: str | None
    ) -> int: ...


class EventSink(Protocol):
    async def post_event(self, kind: str, payload: dict[str, JsonValue]) -> None: ...


class Guards(Protocol):
    async def check(
        self,
        event: InboundEvent,
        decision: TurnDecision,
        *,
        conversation_id: UUID,
        stable_key: str,
    ) -> "GuardVerdictLike": ...


class GuardVerdictLike(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def reason(self) -> str: ...

    @property
    def refusal_text(self) -> str: ...


class ModerationHook(Protocol):
    async def allows(self, text: str) -> bool: ...


class TraceSink(Protocol):
    async def record(
        self,
        *,
        trace_id: str,
        stage: str,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> UUID: ...


MODERATION_NOTICE = "回复触发了内容策略, 已停止发送。"


@dataclass(slots=True)
class AgentWorkerService:
    """LifecycleService consuming the ingest stream and planning replies."""

    consumer: StreamConsumer
    outbound: StreamPublisher
    conversations: ConversationStore
    messages: MessageStore
    readiness: ReadinessSource
    agent: AgentEngine
    capabilities: Mapping[Platform, PlatformCapabilities]
    memory: MemoryCommands | None = None
    events: EventSink | None = None
    guards: Guards | None = None
    moderation: ModerationHook | None = None
    moderation_notice: str = MODERATION_NOTICE
    traces: TraceSink | None = None
    _conversation_locks: dict[str, asyncio.Lock] = field(default_factory=dict[str, asyncio.Lock])

    async def run(self, mode: ProcessMode, stop_event: asyncio.Event) -> None:
        logger.info("agent_worker_started", mode=mode.value)
        try:
            await self.consumer.run(
                self.handle_payload,
                stop_event=stop_event,
                dedupe_key=_envelope_dedupe_key,
            )
        finally:
            logger.info("agent_worker_stopped", mode=mode.value)

    async def handle_payload(self, payload: str) -> None:
        event = InboundEvent.model_validate_json(payload)
        envelope = event.envelope
        key = ConversationKey(
            connection_id=envelope.connection_id,
            chat_kind=envelope.chat_kind,
            chat_id=envelope.chat_id,
            thread_id=envelope.thread_id,
        )
        # Serialize turns per conversation so one slow turn cannot reorder a chat.
        async with self._lock_for(key.stable_key):
            await self._handle_serialized(event, key)

    async def _handle_serialized(self, event: InboundEvent, key: ConversationKey) -> None:
        envelope = event.envelope
        conversation = await self.conversations.get_or_create(
            key, platform=envelope.platform, ephemeral=envelope.ephemeral
        )
        stored = await self.messages.record_inbound(conversation.id, envelope)
        if stored.duplicate:
            logger.info("inbound_duplicate_skipped", envelope_id=envelope.id)
            return
        await self._trace(
            envelope,
            conversation.id,
            "ingest.persist",
            message_id=stored.id,
            attributes={
                "platform": envelope.platform.value,
                "ephemeral": envelope.ephemeral,
                "segments": len(envelope.segments),
            },
        )
        own_ids = await self.messages.recent_outbound_platform_ids(conversation.id)
        decision = decide_turn(
            envelope,
            mentions_self=event.mentions_self,
            replies_to_self=event.replies_to_self,
            own_recent_platform_message_ids=own_ids,
        )
        logger.info(
            "turn_decided",
            envelope_id=envelope.id,
            action=decision.action.value,
            trigger=decision.trigger.value,
            reason=decision.reason,
        )
        await self._trace(
            envelope,
            conversation.id,
            "turn.decision",
            attributes={
                "action": decision.action.value,
                "trigger": decision.trigger.value,
                "reason": decision.reason,
            },
        )
        await self._post_event(envelope, decision)
        if decision.action is TurnAction.IGNORE:
            return
        capabilities = self.capabilities.get(envelope.platform, PlatformCapabilities())
        refusal_text = await self._guard_refusal(event, decision, conversation.id, key)
        if refusal_text == "":
            return  # guard says ignore silently
        is_agent_turn = decision.action is TurnAction.AGENT and refusal_text is None
        if refusal_text is not None:
            from mybot.contracts import TypingProfile

            plan = ReplyPlan(
                text_segments=(refusal_text,),
                typing=TypingProfile(enabled=capabilities.typing),
            )
        elif is_agent_turn:
            from mybot.infrastructure.model_routing import (
                reset_model_conversation,
                set_model_conversation,
            )

            model_context = set_model_conversation(str(conversation.id))
            try:
                plan = await self.agent.run_turn(
                    conversation_id=conversation.id,
                    stable_key=key.stable_key,
                    envelope=envelope,
                    decision=decision,
                    inbound_message_id=stored.id,
                    capabilities=capabilities,
                )
            finally:
                reset_model_conversation(model_context)
        else:
            command = extract_command(envelope)
            if command == "forget":
                plan = await self._handle_forget(envelope, key, capabilities)
            else:
                readiness = await self._readiness_report() if command == "status" else None
                plan = build_direct_reply(
                    envelope,
                    command=command,
                    readiness=readiness,
                    capabilities=capabilities,
                )
        plan = await self._moderated(plan, capabilities, envelope, conversation.id)
        outbound_id = await self.messages.record_outbound(
            conversation.id, plan, trace_id=envelope.trace_id
        )
        message = OutboundMessage(
            internal_message_id=outbound_id,
            platform=envelope.platform,
            connection_id=envelope.connection_id,
            chat_kind=envelope.chat_kind,
            chat_id=envelope.chat_id,
            reply_plan=plan,
            reply_to_platform_message_id=(
                platform_message_id_from_envelope(envelope) if capabilities.replies else None
            ),
            trace_id=envelope.trace_id,
        )
        publish_started = monotonic()
        await self.outbound.publish(message.model_dump_json())
        await self._trace(
            envelope,
            conversation.id,
            "outbound.publish",
            duration_ms=int((monotonic() - publish_started) * 1_000),
            message_id=outbound_id,
            attributes={
                "text_segments": len(plan.text_segments),
                "media_segments": len(plan.media_segments),
            },
        )
        if is_agent_turn:
            from mybot.infrastructure.model_routing import (
                reset_model_conversation,
                set_model_conversation,
            )

            model_context = set_model_conversation(str(conversation.id))
            try:
                await self.agent.after_reply(
                    envelope=envelope,
                    stable_key=key.stable_key,
                    reply_text="\n\n".join(plan.text_segments),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("after_reply_hook_failed", envelope_id=envelope.id)
            finally:
                reset_model_conversation(model_context)

    async def _guard_refusal(
        self,
        event: InboundEvent,
        decision: TurnDecision,
        conversation_id: UUID,
        key: ConversationKey,
    ) -> str | None:
        """None = allow; "" = ignore silently; other = refuse with this text."""

        if self.guards is None:
            return None
        try:
            verdict = await self.guards.check(
                event,
                decision,
                conversation_id=conversation_id,
                stable_key=key.stable_key,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guard_check_failed", envelope_id=event.envelope.id)
            return None  # guards are best-effort; availability wins
        if verdict.kind == "ignore":
            logger.info(
                "turn_guard_ignored",
                envelope_id=event.envelope.id,
                reason=verdict.reason,
            )
            return ""
        if verdict.kind == "refuse":
            logger.info(
                "turn_guard_refused",
                envelope_id=event.envelope.id,
                reason=verdict.reason,
            )
            return verdict.refusal_text
        return None

    async def _moderated(
        self,
        plan: ReplyPlan,
        capabilities: PlatformCapabilities,
        envelope: MessageEnvelope,
        conversation_id: UUID,
    ) -> ReplyPlan:
        if self.moderation is None:
            await self._trace(
                envelope,
                conversation_id,
                "moderation.outbound",
                status="skipped",
                attributes={"reason": "disabled"},
            )
            return plan
        started = monotonic()
        try:
            approved = await self.moderation.allows("\n\n".join(plan.text_segments))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("moderation_hook_failed")
            await self._trace(
                envelope,
                conversation_id,
                "moderation.outbound",
                status="error",
                duration_ms=int((monotonic() - started) * 1_000),
                attributes={"error_code": "moderation_hook_failed"},
            )
            return plan
        if approved:
            await self._trace(
                envelope,
                conversation_id,
                "moderation.outbound",
                duration_ms=int((monotonic() - started) * 1_000),
                attributes={"approved": True},
            )
            return plan
        from mybot.contracts import TypingProfile

        logger.warning("reply_withheld_by_moderation")
        await self._trace(
            envelope,
            conversation_id,
            "moderation.outbound",
            status="error",
            duration_ms=int((monotonic() - started) * 1_000),
            attributes={"approved": False},
        )
        return ReplyPlan(
            text_segments=(self.moderation_notice,),
            typing=TypingProfile(enabled=capabilities.typing),
        )

    async def _post_event(self, envelope: MessageEnvelope, decision: TurnDecision) -> None:
        """Read-only turn notification for plugin event hooks; never blocks a reply."""

        if self.events is None:
            return
        try:
            await self.events.post_event(
                "message",
                {
                    "envelope": envelope.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("plugin_event_post_failed", envelope_id=envelope.id)

    async def _handle_forget(
        self,
        envelope: MessageEnvelope,
        key: ConversationKey,
        capabilities: PlatformCapabilities,
    ) -> ReplyPlan:
        from mybot.contracts import ChatKind
        from mybot.engine.direct_replies import forget_reply

        if self.memory is None:
            return forget_reply(None, capabilities=capabilities)
        conversation_scope = key.stable_key if envelope.chat_kind is ChatKind.DIRECT else None
        revoked = await self.memory.forget(
            subject_identity_id=envelope.sender_identity_id,
            conversation_stable_key=conversation_scope,
        )
        logger.info(
            "memories_forgotten",
            subject=envelope.sender_identity_id,
            revoked=revoked,
        )
        return forget_reply(revoked, capabilities=capabilities)

    def _lock_for(self, stable_key: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(stable_key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[stable_key] = lock
        return lock

    async def _readiness_report(self) -> ReadinessResponse | None:
        try:
            return await self.readiness.check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("status_readiness_probe_failed")
            return None

    async def _trace(
        self,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        stage: str,
        *,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
        message_id: UUID | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self.traces is None:
            return
        try:
            await self.traces.record(
                trace_id=envelope.trace_id,
                stage=stage,
                status=status,
                duration_ms=duration_ms,
                conversation_id=conversation_id,
                message_id=message_id,
                attributes=attributes,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("trace_span_record_failed", stage=stage)


def _envelope_dedupe_key(payload: str) -> str:
    return InboundEvent.model_validate_json(payload).envelope.id


def create_agent_worker_service(settings: Settings) -> AgentWorkerService:
    """Build the production worker from settings-backed Redis and PostgreSQL."""

    import httpx

    from mybot.engine.agent_turns import AgentTurnEngine
    from mybot.engine.memory_service import MemoryService
    from mybot.engine.vision import VisionMode, VisionService
    from mybot.infrastructure.budget import TokenBudget
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.model_routing import (
        ModelPurpose,
        ModelRouter,
        RedisModelCooldowns,
        legacy_model_channels,
        openai_client_factory,
    )
    from mybot.repositories.conversations import ConversationRepository
    from mybot.repositories.llm_calls import LlmCallLogRepository
    from mybot.repositories.memory import MemoryRepository
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.system_kv import SystemKvRepository
    from mybot.repositories.tool_invocations import ToolInvocationRepository
    from mybot.repositories.traces import TraceSpanRepository
    from mybot.repositories.turns import TurnRepository
    from mybot.tools import ToolExecutor, ToolRegistry
    from mybot.tools.approvals import SystemKvApprovals
    from mybot.tools.fetch import UrlFetchTool
    from mybot.tools.plugins import BrokerEventSink, BrokerToolCatalog
    from mybot.tools.search import SearxngSearchTool

    backend = create_redis_backend(settings.redis_url.get_secret_value())
    sessions = create_session_factory(create_database_engine(settings))
    messages = MessageRepository(sessions)
    traces = TraceSpanRepository(sessions)
    consumer = StreamConsumer(
        backend,
        stream=settings.ingest_stream,
        group=settings.ingest_group,
        consumer=f"agent-worker-{os.getpid()}",
        dead_letter_stream=f"{settings.ingest_stream}:dead",
        max_attempts=settings.stream_delivery_max_attempts,
        dedupe_ttl_seconds=settings.stream_dedupe_ttl_seconds,
        dedupe_prefix="mybot:seen:ingest",
        block_ms=settings.stream_block_ms,
        claim_min_idle_ms=settings.stream_claim_min_idle_ms,
    )
    config = SystemKvRepository(sessions)
    def secret_lookup(name: str) -> str | None:
        return settings.model_secret(name)

    model_http = httpx.AsyncClient()
    from redis.asyncio import Redis

    model_router = ModelRouter(
        config=config,
        fallback_channels=legacy_model_channels(settings),
        cooldowns=RedisModelCooldowns(
            Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                settings.redis_url.get_secret_value(), decode_responses=True
            )
        ),
        attempts=LlmCallLogRepository(sessions),
        client_factory=openai_client_factory(
            model_http,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        secret_lookup=secret_lookup,
        cache_ttl_seconds=settings.model_channels_cache_ttl_seconds,
        cooldown_seconds=settings.model_channel_cooldown_seconds,
    )
    llm = model_router.for_purpose(ModelPurpose.CHAT)
    memory: MemoryService | None = None
    if settings.memory_enabled:
        memory = MemoryService(
            embeddings=model_router.embeddings(),
            store=MemoryRepository(sessions),
            llm=model_router.for_purpose(ModelPurpose.MEMORY),
            embedding_model=settings.embedding_model,
            min_confidence=settings.memory_min_confidence,
            retrieval_limit=settings.memory_retrieval_limit,
            token_budget=settings.memory_token_budget,
        )
    tool_http_timeout = httpx.Timeout(min(settings.tool_timeout_seconds, 30.0))
    registry = ToolRegistry(
        [
            SearxngSearchTool(
                client=httpx.AsyncClient(timeout=tool_http_timeout),
                searxng_url=settings.searxng_url,
                max_results=settings.search_max_results,
            ),
            UrlFetchTool(
                client=httpx.AsyncClient(timeout=tool_http_timeout, follow_redirects=True),
                max_bytes=settings.tool_fetch_max_bytes,
            ),
        ]
    )
    catalog: ToolRegistry | BrokerToolCatalog = registry
    events: BrokerEventSink | None = None
    if settings.plugin_broker_url is not None:
        broker_client = httpx.AsyncClient()
        catalog = BrokerToolCatalog(
            static=registry,
            client=broker_client,
            broker_url=settings.plugin_broker_url,
            ttl_seconds=settings.plugin_catalog_ttl_seconds,
            invoke_timeout_seconds=settings.plugin_invoke_timeout_seconds,
        )
        events = BrokerEventSink(client=broker_client, broker_url=settings.plugin_broker_url)
    agent = AgentTurnEngine(
        llm=llm,
        persona=config,
        history=messages,
        turns=TurnRepository(sessions),
        budget=TokenBudget(
            backend=backend,
            global_daily_ceiling=settings.agent_daily_token_ceiling,
            conversation_daily_ceiling=settings.agent_conversation_daily_token_ceiling,
        ),
        default_system_prompt=settings.agent_system_prompt,
        llm_model_name=settings.llm_model,
        history_max_messages=settings.agent_history_max_messages,
        history_token_budget=settings.agent_history_token_budget,
        tools=catalog,
        executor=ToolExecutor(
            catalog,
            timeout_seconds=settings.tool_timeout_seconds,
            approvals=SystemKvApprovals(
                config=config,
                ttl_seconds=settings.tool_approvals_cache_ttl_seconds,
            ),
        ),
        granted_capabilities=settings.granted_capabilities(),
        max_tool_calls=settings.tool_max_calls_per_turn,
        turn_deadline_seconds=settings.turn_deadline_seconds,
        invocations=ToolInvocationRepository(sessions),
        memory=memory,
        vision=VisionService(
            llm=model_router.for_purpose(ModelPurpose.VISION),
            mode=VisionMode(settings.vision_mode),
            max_description_chars=settings.vision_max_description_chars,
        ),
        not_configured_fallback=settings.fallback_not_configured,
        llm_failure_fallback=settings.fallback_llm_failure,
        budget_fallback=settings.fallback_budget_exceeded,
        empty_reply_fallback=settings.fallback_empty_reply,
        traces=traces,
    )
    from mybot.engine.guards import TurnGuards

    return AgentWorkerService(
        consumer=consumer,
        outbound=StreamPublisher(
            backend=backend,
            stream=settings.outbound_stream,
            maxlen=settings.stream_maxlen,
        ),
        conversations=ConversationRepository(sessions),
        messages=messages,
        readiness=create_readiness_service(settings),
        agent=agent,
        capabilities=DEFAULT_CAPABILITIES,
        memory=memory,
        events=events,
        guards=TurnGuards(
            counters=backend,
            history=messages,
            user_per_minute=settings.rate_limit_user_per_minute,
            chat_per_minute=settings.rate_limit_chat_per_minute,
            group_cooldown_seconds=settings.group_cooldown_seconds,
            input_max_chars=settings.input_max_chars,
            loop_guard_enabled=settings.loop_guard_enabled,
        ),
        moderation_notice=settings.fallback_moderation,
        traces=traces,
    )
