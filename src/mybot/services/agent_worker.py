"""The agent-worker lifecycle: consume, persist, decide, and plan replies."""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.adapters.qq.translate import QQ_CAPABILITIES
from mybot.adapters.telegram.translate import TELEGRAM_CAPABILITIES
from mybot.contracts import (
    AnnotationMatch,
    ChatKind,
    ConversationKey,
    MessageEnvelope,
    ModerationAction,
    ModerationDecision,
    ModerationPoint,
    ModerationRequest,
    Platform,
    PlatformCapabilities,
    ReplyPlan,
    ReplyWillingnessPolicy,
    TurnAction,
    TurnDecision,
    TurnTrigger,
    WillingnessScore,
)
from mybot.engine.agent_turns import AgentRuntime
from mybot.engine.direct_replies import build_direct_reply
from mybot.engine.turn_policy import decide_turn, extract_command
from mybot.infrastructure.health import ReadinessResponse, create_readiness_service
from mybot.infrastructure.leases import ConversationLease, LeaseManager, MemoryLeaseBackend
from mybot.infrastructure.streams import (
    StreamConsumer,
    StreamPublisher,
    create_redis_backend,
    current_delivery_attempt,
)
from mybot.infrastructure.telemetry import start_span
from mybot.repositories.conversations import ConversationRecord
from mybot.repositories.messages import (
    StoredMessage,
    platform_message_id_from_envelope,
)
from mybot.repositories.profiles import ResolvedProfile
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
        runtime: AgentRuntime | None = None,
    ) -> ReplyPlan: ...

    async def after_reply(
        self,
        *,
        envelope: MessageEnvelope,
        stable_key: str,
        reply_text: str,
        runtime: AgentRuntime | None = None,
    ) -> None: ...


class ProfileSource(Protocol):
    async def resolve(
        self,
        conversation_id: UUID,
        *,
        default_system_prompt: str,
        default_tool_capabilities: tuple[str, ...],
    ) -> ResolvedProfile: ...


class WillingnessEngine(Protocol):
    async def evaluate(
        self,
        *,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        message_id: UUID | None,
        profile_id: UUID | None,
        persona: str,
        policy: ReplyWillingnessPolicy,
    ) -> WillingnessScore: ...


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
    async def moderate(
        self,
        request: ModerationRequest,
        *,
        conversation_stable_key: str | None = None,
        message_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> ModerationDecision: ...


class AnnotationEngine(Protocol):
    async def match(
        self,
        query: str,
        *,
        conversation_id: UUID,
        allow_conversation: bool,
    ) -> AnnotationMatch | None: ...


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


class EvaluationSink(Protocol):
    async def complete_shadow(
        self,
        *,
        result_id: UUID,
        conversation_id: UUID,
        inbound_message_id: UUID,
        response: str,
        citations: tuple[str, ...],
    ) -> None: ...


class AccessDecisionLike(Protocol):
    @property
    def allowed(self) -> bool: ...

    @property
    def reply_text(self) -> str | None: ...

    @property
    def reason(self) -> str: ...


class InboundAccess(Protocol):
    async def check(self, envelope: MessageEnvelope) -> AccessDecisionLike: ...


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
    profiles: ProfileSource | None = None
    willingness: WillingnessEngine | None = None
    default_system_prompt: str = "You are MyBot."
    default_tool_capabilities: tuple[str, ...] = ()
    memory: MemoryCommands | None = None
    events: EventSink | None = None
    guards: Guards | None = None
    moderation: ModerationHook | None = None
    annotations: AnnotationEngine | None = None
    moderation_notice: str = MODERATION_NOTICE
    traces: TraceSink | None = None
    access: InboundAccess | None = None
    evaluations: EvaluationSink | None = None
    leases: ConversationLease = field(
        default_factory=lambda: LeaseManager(MemoryLeaseBackend())
    )

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
        with start_span(
            "agent.turn",
            trace_id=envelope.trace_id,
            attributes={
                "mybot.platform": envelope.platform.value,
                "mybot.chat_kind": envelope.chat_kind.value,
            },
        ):
            # A renewable Redis lease preserves per-conversation order across replicas.
            await self.leases.run(
                key.stable_key,
                lambda: self._handle_serialized(event, key),
            )

    async def _handle_serialized(self, event: InboundEvent, key: ConversationKey) -> None:
        envelope = event.envelope
        if self.access is not None:
            access = await self.access.check(envelope)
            if not access.allowed:
                if access.reply_text:
                    await self._publish_access_reply(envelope, access.reply_text)
                logger.info(
                    "inbound_access_denied",
                    platform=envelope.platform.value,
                    subject_identity_id=envelope.sender_identity_id,
                    reason=access.reason,
                )
                return
        inbound_decision = await self._moderate_inbound(envelope, key)
        if inbound_decision is not None:
            if inbound_decision.action is ModerationAction.DIRECT_OUTPUT:
                response = inbound_decision.preset_response or self.moderation_notice
                await self._publish_access_reply(envelope, response)
                return
            envelope = _override_envelope_text(
                envelope,
                inbound_decision.preset_response or self.moderation_notice,
            )
            event = event.model_copy(update={"envelope": envelope})
        conversation = await self.conversations.get_or_create(
            key, platform=envelope.platform, ephemeral=envelope.ephemeral
        )
        stored = await self.messages.record_inbound(conversation.id, envelope)
        if stored.duplicate and current_delivery_attempt() <= 1:
            logger.info("inbound_duplicate_skipped", envelope_id=envelope.id)
            return
        if stored.duplicate:
            logger.warning(
                "inbound_retry_resumed",
                envelope_id=envelope.id,
                delivery_attempt=current_delivery_attempt(),
            )
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
        resolved_profile = await self._resolve_profile(conversation.id)
        runtime = (
            AgentRuntime(
                system_prompt=resolved_profile.persona.system_prompt,
                granted_capabilities=resolved_profile.profile.tool_capabilities,
                memory_enabled=resolved_profile.profile.memory.enabled,
                memory_retrieval_limit=resolved_profile.profile.memory.retrieval_limit,
                expression_examples=resolved_profile.profile.memory.expression_examples,
                relationship_enabled=resolved_profile.profile.memory.relationship_enabled,
            )
            if resolved_profile is not None
            else None
        )
        decision = decide_turn(
            envelope,
            mentions_self=event.mentions_self,
            replies_to_self=event.replies_to_self,
            own_recent_platform_message_ids=own_ids,
        )
        decision = await self._apply_willingness(
            envelope=envelope,
            conversation_id=conversation.id,
            message_id=stored.id,
            decision=decision,
            profile=resolved_profile,
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
        annotation_hit = False
        plan: ReplyPlan | None = None
        if refusal_text is not None:
            from mybot.contracts import TypingProfile

            plan = ReplyPlan(
                text_segments=(refusal_text,),
                typing=TypingProfile(enabled=capabilities.typing),
            )
        elif is_agent_turn:
            from mybot.engine.prompt import envelope_text
            from mybot.engine.reply_shaping import shape_reply

            matched = None
            query = envelope_text(envelope)
            if self.annotations is not None and query:
                matched = await self.annotations.match(
                    query,
                    conversation_id=conversation.id,
                    allow_conversation=not envelope.ephemeral,
                )
            if matched is not None:
                annotation_hit = True
                plan = shape_reply(matched.answer, capabilities=capabilities)
                await self._trace(
                    envelope,
                    conversation.id,
                    "annotation.match",
                    attributes={
                        "annotation_id": str(matched.annotation_id),
                        "score": matched.score,
                        "threshold": matched.threshold,
                    },
                )
            else:
                await self._trace(
                    envelope,
                    conversation.id,
                    "annotation.match",
                    status="skipped",
                    attributes={"reason": "no_high_confidence_match"},
                )
            from mybot.infrastructure.model_routing import (
                reset_model_conversation,
                reset_model_profile,
                set_model_conversation,
                set_model_profile,
            )

            model_context = (
                set_model_conversation(str(conversation.id)) if not annotation_hit else None
            )
            profile_context = (
                set_model_profile(
                    tier=resolved_profile.profile.model_tier,
                    profile_id=str(resolved_profile.profile.id),
                    persona_version_id=str(resolved_profile.persona.id),
                )
                if resolved_profile is not None and not annotation_hit
                else None
            )
            try:
                if not annotation_hit:
                    plan = await self.agent.run_turn(
                        conversation_id=conversation.id,
                        stable_key=key.stable_key,
                        envelope=envelope,
                        decision=decision,
                        inbound_message_id=stored.id,
                        capabilities=capabilities,
                        runtime=runtime,
                    )
            finally:
                if profile_context is not None:
                    reset_model_profile(profile_context)
                if model_context is not None:
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
        assert plan is not None
        plan = await self._moderated(plan, capabilities, envelope, conversation.id)
        evaluation_result_id = _evaluation_result_id(envelope)
        if evaluation_result_id is not None and self.evaluations is not None:
            await self.evaluations.complete_shadow(
                result_id=evaluation_result_id,
                conversation_id=conversation.id,
                inbound_message_id=stored.id,
                response="\n\n".join(plan.text_segments),
                citations=tuple(citation.uri for citation in plan.citations),
            )
            await self._trace(
                envelope,
                conversation.id,
                "evaluation.shadow",
                message_id=stored.id,
                attributes={"result_id": str(evaluation_result_id)},
            )
            return
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
        if is_agent_turn and not annotation_hit:
            from mybot.infrastructure.model_routing import (
                reset_model_conversation,
                reset_model_profile,
                set_model_conversation,
                set_model_profile,
            )

            model_context = set_model_conversation(str(conversation.id))
            profile_context = (
                set_model_profile(
                    tier=resolved_profile.profile.model_tier,
                    profile_id=str(resolved_profile.profile.id),
                    persona_version_id=str(resolved_profile.persona.id),
                )
                if resolved_profile is not None
                else None
            )
            try:
                await self.agent.after_reply(
                    envelope=envelope,
                    stable_key=key.stable_key,
                    reply_text="\n\n".join(plan.text_segments),
                    runtime=runtime,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("after_reply_hook_failed", envelope_id=envelope.id)
            finally:
                if profile_context is not None:
                    reset_model_profile(profile_context)
                reset_model_conversation(model_context)

    async def _publish_access_reply(self, envelope: MessageEnvelope, text: str) -> None:
        message = OutboundMessage(
            internal_message_id=uuid4(),
            platform=envelope.platform,
            connection_id=envelope.connection_id,
            chat_kind=envelope.chat_kind,
            chat_id=envelope.chat_id,
            reply_plan=ReplyPlan(text_segments=(text,)),
            reply_to_platform_message_id=platform_message_id_from_envelope(envelope),
            trace_id=envelope.trace_id,
        )
        await self.outbound.publish(message.model_dump_json())

    async def _resolve_profile(self, conversation_id: UUID) -> ResolvedProfile | None:
        if self.profiles is None:
            return None
        try:
            return await self.profiles.resolve(
                conversation_id,
                default_system_prompt=self.default_system_prompt,
                default_tool_capabilities=self.default_tool_capabilities,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("profile_resolution_failed", conversation_id=str(conversation_id))
            return None

    async def _apply_willingness(
        self,
        *,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        message_id: UUID | None,
        decision: TurnDecision,
        profile: ResolvedProfile | None,
    ) -> TurnDecision:
        if (
            decision.action is not TurnAction.IGNORE
            or envelope.chat_kind is ChatKind.DIRECT
            or envelope.ephemeral
            or profile is None
            or not profile.profile.willingness.enabled
            or self.willingness is None
        ):
            return decision
        try:
            score = await self.willingness.evaluate(
                envelope=envelope,
                conversation_id=conversation_id,
                message_id=message_id,
                profile_id=profile.profile.id,
                persona=profile.persona.system_prompt,
                policy=profile.profile.willingness,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reply_willingness_failed", conversation_id=str(conversation_id))
            return decision
        if not score.allowed:
            return TurnDecision(
                action=TurnAction.IGNORE,
                reason=f"willingness blocked: {score.score:.4f} < {score.threshold:.4f}",
                confidence=1.0 - score.score,
                trigger=TurnTrigger.POLICY,
            )
        return TurnDecision(
            action=TurnAction.AGENT,
            reason=f"willingness allowed: {score.reason} ({score.score:.4f})",
            confidence=score.score,
            trigger=TurnTrigger.POLICY,
        )

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
            decision = await self.moderation.moderate(
                ModerationRequest.model_validate(
                    {
                        "point": ModerationPoint.OUTBOUND,
                        "params": {"text": "\n\n".join(plan.text_segments)},
                    }
                ),
                conversation_stable_key=ConversationKey(
                    connection_id=envelope.connection_id,
                    chat_kind=envelope.chat_kind,
                    chat_id=envelope.chat_id,
                    thread_id=envelope.thread_id,
                ).stable_key,
                trace_id=envelope.trace_id,
            )
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
        if not decision.flagged:
            await self._trace(
                envelope,
                conversation_id,
                "moderation.outbound",
                duration_ms=int((monotonic() - started) * 1_000),
                attributes={"approved": True, "backend": decision.backend},
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
            attributes={
                "approved": False,
                "backend": decision.backend,
                "action": decision.action.value,
            },
        )
        return ReplyPlan(
            text_segments=(decision.preset_response or self.moderation_notice,),
            typing=TypingProfile(enabled=capabilities.typing),
        )

    async def _moderate_inbound(
        self, envelope: MessageEnvelope, key: ConversationKey
    ) -> ModerationDecision | None:
        if self.moderation is None:
            return None
        from mybot.engine.prompt import envelope_text

        started = monotonic()
        try:
            decision = await self.moderation.moderate(
                ModerationRequest.model_validate(
                    {
                        "point": ModerationPoint.INBOUND,
                        "params": {
                            "text": envelope_text(envelope),
                            "sender_identity_id": envelope.sender_identity_id,
                            "platform": envelope.platform.value,
                        },
                    }
                ),
                conversation_stable_key=key.stable_key,
                trace_id=envelope.trace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("inbound_moderation_hook_failed")
            return None
        if not decision.flagged:
            return None
        logger.warning(
            "inbound_message_flagged",
            backend=decision.backend,
            action=decision.action.value,
            duration_ms=int((monotonic() - started) * 1_000),
        )
        return decision

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


def _override_envelope_text(envelope: MessageEnvelope, text: str) -> MessageEnvelope:
    from mybot.contracts import TextSegment

    remaining = tuple(segment for segment in envelope.segments if segment.type != "text")
    return envelope.model_copy(update={"segments": (TextSegment(text=text), *remaining)})


def _evaluation_result_id(envelope: MessageEnvelope) -> UUID | None:
    from collections.abc import Mapping

    raw = envelope.raw_ref
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("evaluation_result_id")
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def create_agent_worker_service(settings: Settings) -> AgentWorkerService:
    """Build the production worker from settings-backed Redis and PostgreSQL."""

    import httpx

    from mybot.engine.agent_turns import AgentTurnEngine
    from mybot.engine.knowledge import AnnotationMatcher, KnowledgeSearchService
    from mybot.engine.memory_service import MemoryService
    from mybot.engine.vision import VisionMode, VisionService
    from mybot.engine.willingness import ReplyWillingnessScorer
    from mybot.infrastructure.budget import TokenBudget
    from mybot.infrastructure.database import create_database_engine, create_session_factory
    from mybot.infrastructure.leases import create_redis_lease_manager
    from mybot.infrastructure.model_routing import (
        ModelPurpose,
        ModelRouter,
        RedisModelCooldowns,
        legacy_model_channels,
        openai_client_factory,
    )
    from mybot.repositories.conversations import ConversationRepository
    from mybot.repositories.core_memory import CoreBlockRepository
    from mybot.repositories.knowledge import KnowledgeRepository
    from mybot.repositories.llm_calls import LlmCallLogRepository
    from mybot.repositories.memory import MemoryRepository
    from mybot.repositories.messages import MessageRepository
    from mybot.repositories.pairing import PairingRepository
    from mybot.repositories.participation import WillingnessAuditRepository
    from mybot.repositories.profiles import ProfileRepository
    from mybot.repositories.system_kv import SystemKvRepository
    from mybot.repositories.tool_invocations import ToolInvocationRepository
    from mybot.repositories.traces import TraceSpanRepository
    from mybot.repositories.turns import TurnRepository
    from mybot.security.pairing import PairingGate
    from mybot.skills import SkillStore
    from mybot.tools import Tool, ToolExecutor, ToolRegistry
    from mybot.tools.approvals import DatabaseToolApprovals, SystemKvApprovals
    from mybot.tools.fetch import UrlFetchTool
    from mybot.tools.knowledge import KnowledgeSearchTool
    from mybot.tools.memory import MemoryAppendTool, MemoryReplaceTool
    from mybot.tools.plugins import BrokerEventSink, BrokerToolCatalog
    from mybot.tools.search import SearxngSearchTool
    from mybot.tools.skills import LoadSkillTool

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
    moderation = None
    if settings.moderation_enabled:
        from mybot.repositories.safety import ModerationAuditRepository
        from mybot.security.moderation import (
            LocalKeywordModeration,
            ModerationBackend,
            ModerationService,
            OpenAIModerationBackend,
            PluginModerationBackend,
        )

        moderation_backends: dict[str, ModerationBackend] = {
            "local": LocalKeywordModeration()
        }
        if settings.moderation_api_base_url is not None:
            moderation_backends["api"] = OpenAIModerationBackend(
                client=httpx.AsyncClient(timeout=settings.moderation_timeout_seconds),
                base_url=settings.moderation_api_base_url,
                api_key=(
                    settings.moderation_api_key.get_secret_value()
                    if settings.moderation_api_key is not None
                    else None
                ),
                model=settings.moderation_api_model,
            )
        if settings.plugin_broker_url is not None:
            moderation_backends["plugin"] = PluginModerationBackend(
                client=httpx.AsyncClient(timeout=settings.moderation_timeout_seconds),
                broker_url=settings.plugin_broker_url,
            )
        moderation = ModerationService(
            policies=config,
            backends=moderation_backends,
            audit=ModerationAuditRepository(sessions),
            timeout_seconds=settings.moderation_timeout_seconds,
        )
    profiles = ProfileRepository(sessions, legacy_persona=config)
    def secret_lookup(name: str) -> str | None:
        return settings.model_secret(name)

    model_http = httpx.AsyncClient()
    telegram_image_resolver = None
    if settings.telegram_bot_token is not None:
        from mybot.adapters.telegram.files import TelegramImageResolver

        telegram_image_resolver = TelegramImageResolver(
            token=settings.telegram_bot_token.get_secret_value(),
            client=model_http,
            api_base_url=settings.telegram_api_base_url,
            max_bytes=settings.vision_max_image_bytes,
            timeout_seconds=settings.vision_image_download_timeout_seconds,
        )
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
    core_memory = CoreBlockRepository(sessions)
    knowledge = KnowledgeRepository(sessions)
    from mybot.evaluation import ShadowEvaluationSink
    from mybot.repositories.safety import EvaluationRepository, ToolApprovalRepository

    approval_repository = ToolApprovalRepository(sessions)
    evaluation_repository = EvaluationRepository(sessions)
    if settings.memory_enabled:
        memory = MemoryService(
            embeddings=model_router.embeddings(),
            store=MemoryRepository(sessions),
            llm=model_router.for_purpose(ModelPurpose.MEMORY),
            core_store=core_memory,
            flush_once=backend,
            flush_enabled=settings.memory_flush_enabled,
            flush_debounce_ttl_seconds=settings.memory_flush_debounce_ttl_seconds,
            flush_max_messages=settings.memory_flush_max_messages,
            flush_token_budget=settings.memory_flush_token_budget,
            min_confidence=settings.memory_min_confidence,
            retrieval_limit=settings.memory_retrieval_limit,
            token_budget=settings.memory_token_budget,
        )
    tool_http_timeout = httpx.Timeout(min(settings.tool_timeout_seconds, 30.0))
    skill_store = SkillStore(settings.skills_dir)
    builtin_tools: list[Tool] = [
        LoadSkillTool(skill_store),
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
    if settings.knowledge_enabled:
        builtin_tools.append(
            KnowledgeSearchTool(
                service=KnowledgeSearchService(
                    store=knowledge,
                    embeddings=model_router.embeddings(),
                ),
                sandbox_connection_id=settings.sandbox_connection_id,
            )
        )
    if settings.memory_enabled:
        builtin_tools.extend(
            [
                MemoryAppendTool(
                    repository=core_memory,
                    persona_token_budget=settings.memory_core_persona_token_budget,
                    user_profile_token_budget=(
                        settings.memory_core_user_profile_token_budget
                    ),
                    approval_required=settings.memory_core_tools_approval_required,
                ),
                MemoryReplaceTool(
                    repository=core_memory,
                    persona_token_budget=settings.memory_core_persona_token_budget,
                    user_profile_token_budget=(
                        settings.memory_core_user_profile_token_budget
                    ),
                    approval_required=settings.memory_core_tools_approval_required,
                ),
            ]
        )
    registry = ToolRegistry(
        builtin_tools
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
        history_flush_max_messages=settings.memory_flush_max_messages,
        tools=catalog,
        executor=ToolExecutor(
            catalog,
            timeout_seconds=settings.tool_timeout_seconds,
            approvals=SystemKvApprovals(
                config=config,
                ttl_seconds=settings.tool_approvals_cache_ttl_seconds,
            ),
            approval_requests=DatabaseToolApprovals(
                repository=approval_repository,
                timeout_seconds=settings.tool_approval_timeout_seconds,
                poll_seconds=settings.tool_approval_poll_seconds,
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
            image_resolver=telegram_image_resolver,
        ),
        vision_llm=model_router.for_purpose(ModelPurpose.VISION),
        skills=skill_store,
        skill_prompt_max_chars=settings.skills_prompt_max_chars,
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
        profiles=profiles,
        willingness=(
            ReplyWillingnessScorer(
                activity=messages,
                semantic=memory,
                audit=WillingnessAuditRepository(sessions),
            )
            if memory is not None
            else None
        ),
        default_system_prompt=settings.agent_system_prompt,
        default_tool_capabilities=settings.granted_capabilities(),
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
        moderation=moderation,
        annotations=(
            AnnotationMatcher(
                store=knowledge,
                embeddings=model_router.embeddings(),
                minimum_margin=settings.annotation_minimum_margin,
            )
            if settings.annotation_enabled
            else None
        ),
        traces=traces,
        access=PairingGate(PairingRepository(sessions)),
        evaluations=ShadowEvaluationSink(
            repository=evaluation_repository,
            judge=model_router.for_purpose(ModelPurpose.CHAT),
        ),
        leases=create_redis_lease_manager(
            settings.redis_url.get_secret_value(),
            ttl_ms=settings.conversation_lease_ttl_ms,
            wait_timeout_seconds=settings.conversation_lease_wait_seconds,
            retry_interval_seconds=settings.conversation_lease_retry_seconds,
        ),
    )
