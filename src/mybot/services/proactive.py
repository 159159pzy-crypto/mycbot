"""Policy-gated, profile-aware LLM heartbeat check-ins."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from mybot.adapters import OutboundMessage
from mybot.contracts import (
    ChatKind,
    MessageEnvelope,
    Platform,
    ReplyPlan,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.infrastructure.model_routing import (
    reset_model_conversation,
    reset_model_profile,
    set_model_conversation,
    set_model_profile,
)
from mybot.repositories.conversations import ConversationDetail
from mybot.repositories.messages import MessageText
from mybot.repositories.profiles import ResolvedProfile
from mybot.repositories.turns import TurnOutcome

logger = structlog.get_logger("mybot.proactive")

OPTIN_KEY = "proactive.enabled_conversations"
HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_SYSTEM_PROMPT = (
    "This is a scheduled heartbeat, not a user request. Decide whether a short message would "
    "be genuinely useful or caring based only on the supplied recent conversation and durable "
    "core memory. If there is no concrete reason to interrupt, return exactly HEARTBEAT_OK. "
    "Otherwise return one brief natural message in the conversation language, referencing a "
    "real recent topic when appropriate. Do not invent events, urgency, or obligations."
)


class ProactiveConfig(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


class ConversationLookup(Protocol):
    async def by_stable_key(self, stable_key: str) -> ConversationDetail | None: ...


class OutboundStore(Protocol):
    async def record_outbound(
        self,
        conversation_id: UUID,
        plan: ReplyPlan,
        *,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID: ...


class RecentMessages(Protocol):
    async def recent_texts(
        self, conversation_id: UUID, *, limit: int = 40
    ) -> tuple[MessageText, ...]: ...


class TurnStore(Protocol):
    async def record_turn(
        self,
        conversation_id: UUID,
        decision: TurnDecision,
        *,
        outcome: TurnOutcome,
        inbound_message_id: UUID | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> UUID: ...


class Publisher(Protocol):
    async def publish(self, payload: str) -> str: ...


class OnceBackend(Protocol):
    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...


class HeartbeatLlm(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


class ProfileLookup(Protocol):
    async def resolve(
        self,
        conversation_id: UUID,
        *,
        default_system_prompt: str,
        default_tool_capabilities: tuple[str, ...],
    ) -> ResolvedProfile: ...


class CoreMemory(Protocol):
    async def core_block(self, envelope: MessageEnvelope) -> str | None: ...


class ProactiveAudit(Protocol):
    async def record(
        self,
        *,
        conversation_id: UUID,
        profile_id: UUID | None,
        persona_version_id: UUID | None,
        outcome: str,
        response_text: str | None,
        model: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: str | None = None,
    ) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """UTC quiet window; start == end means no quiet window; wraps midnight."""

    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@dataclass(slots=True)
class ProactivePass:
    """One scheduled sweep over opted-in conversations."""

    config: ProactiveConfig
    conversations: ConversationLookup
    messages: OutboundStore
    turns: TurnStore
    outbound: Publisher
    once: OnceBackend
    history: RecentMessages | None = None
    llm: HeartbeatLlm | None = None
    profiles: ProfileLookup | None = None
    memory: CoreMemory | None = None
    audit: ProactiveAudit | None = None
    enabled: bool = False
    message: str = "最近有点安静, 有什么想聊或需要我帮忙的吗?"
    default_system_prompt: str = "You are MyBot."
    default_tool_capabilities: tuple[str, ...] = ()
    context_messages: int = 12
    min_interval_hours: float = 24.0
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    key_prefix: str = "mybot:proactive"
    now: Callable[[], datetime] = field(default=_utc_now)

    async def run_pass(self) -> int:
        """Generate and send due check-ins; returns how many were actually sent."""

        if not self.enabled:
            return 0
        current = self.now()
        if in_quiet_hours(current.hour, self.quiet_start_hour, self.quiet_end_hour):
            return 0
        raw = await self.config.get(OPTIN_KEY)
        if not isinstance(raw, list):
            return 0
        sent = 0
        for entry in cast(list[object], raw):
            stable_key = str(entry).strip()
            if not stable_key:
                continue
            detail = await self.conversations.by_stable_key(stable_key)
            if detail is None:
                logger.warning("proactive_conversation_missing", stable_key=stable_key)
                continue
            if detail.ephemeral:
                logger.info("proactive_ephemeral_skipped", stable_key=stable_key)
                continue
            due = await self.once.acquire_once(
                f"{self.key_prefix}:{stable_key}",
                ttl_seconds=max(60, int(self.min_interval_hours * 3600)),
            )
            if not due:
                continue
            try:
                sent += int(await self._generate_and_send(detail))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("proactive_generation_failed", stable_key=stable_key)
                await self._audit(
                    detail,
                    profile=None,
                    outcome="error",
                    response_text=None,
                    reply=None,
                    error_code="heartbeat_generation_failed",
                )
        if sent:
            logger.info("proactive_messages_sent", count=sent)
        return sent

    async def _generate_and_send(self, detail: ConversationDetail) -> bool:
        profile = (
            await self.profiles.resolve(
                detail.id,
                default_system_prompt=self.default_system_prompt,
                default_tool_capabilities=self.default_tool_capabilities,
            )
            if self.profiles is not None
            else None
        )
        recent = (
            await self.history.recent_texts(detail.id, limit=self.context_messages)
            if self.history is not None
            else ()
        )
        if self.llm is None:
            await self._send(detail, self.message, profile=profile, reply=None)
            return True

        heartbeat_envelope = _heartbeat_envelope(detail, recent, self.now())
        core = (
            await self.memory.core_block(heartbeat_envelope)
            if self.memory is not None
            else None
        )
        system_prompt = (
            profile.persona.system_prompt if profile is not None else self.default_system_prompt
        )
        prompt = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="system", content=HEARTBEAT_SYSTEM_PROMPT),
        ]
        if core:
            prompt.append(ChatMessage(role="system", content=core))
        prompt.append(
            ChatMessage(role="user", content=_recent_context(detail, recent))
        )
        conversation_token = set_model_conversation(str(detail.id))
        profile_token = (
            set_model_profile(
                tier=profile.profile.model_tier,
                profile_id=str(profile.profile.id),
                persona_version_id=str(profile.persona.id),
            )
            if profile is not None
            else None
        )
        try:
            reply = await self.llm.complete(prompt)
        except asyncio.CancelledError:
            raise
        except LlmError as error:
            logger.warning("proactive_llm_failed", error=str(error))
            await self._audit(
                detail,
                profile=profile,
                outcome="error",
                response_text=None,
                reply=None,
                error_code="heartbeat_llm_failed",
            )
            return False
        finally:
            if profile_token is not None:
                reset_model_profile(profile_token)
            reset_model_conversation(conversation_token)
        text = reply.text.strip()
        if not text or text.upper() == HEARTBEAT_OK:
            await self._audit(
                detail,
                profile=profile,
                outcome="suppressed",
                response_text=text or None,
                reply=reply,
            )
            return False
        await self._send(detail, text, profile=profile, reply=reply)
        return True

    async def _send(
        self,
        detail: ConversationDetail,
        text: str,
        *,
        profile: ResolvedProfile | None,
        reply: LlmReply | None,
    ) -> None:
        plan = ReplyPlan(text_segments=(text,))
        trace_id = str(uuid4())
        outbound_id = await self.messages.record_outbound(
            detail.id, plan, trace_id=trace_id
        )
        message = OutboundMessage(
            internal_message_id=outbound_id,
            platform=Platform(detail.platform),
            connection_id=detail.connection_id,
            chat_kind=ChatKind(detail.chat_kind),
            chat_id=detail.chat_id,
            reply_plan=plan,
            trace_id=trace_id,
        )
        await self.outbound.publish(message.model_dump_json())
        await self.turns.record_turn(
            detail.id,
            TurnDecision(
                action=TurnAction.DIRECT_REPLY,
                reason="scheduled profile-aware heartbeat",
                confidence=1.0,
                trigger=TurnTrigger.PROACTIVE,
            ),
            outcome="replied",
            model=reply.model if reply is not None else None,
            prompt_tokens=reply.prompt_tokens if reply is not None else 0,
            completion_tokens=reply.completion_tokens if reply is not None else 0,
        )
        await self._audit(
            detail,
            profile=profile,
            outcome="sent",
            response_text=text,
            reply=reply,
        )

    async def _audit(
        self,
        detail: ConversationDetail,
        *,
        profile: ResolvedProfile | None,
        outcome: str,
        response_text: str | None,
        reply: LlmReply | None,
        error_code: str | None = None,
    ) -> None:
        if self.audit is None:
            return
        try:
            await self.audit.record(
                conversation_id=detail.id,
                profile_id=profile.profile.id if profile is not None else None,
                persona_version_id=profile.persona.id if profile is not None else None,
                outcome=outcome,
                response_text=response_text,
                model=reply.model if reply is not None else None,
                prompt_tokens=reply.prompt_tokens if reply is not None else 0,
                completion_tokens=reply.completion_tokens if reply is not None else 0,
                error_code=error_code,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proactive_audit_failed", conversation_id=str(detail.id))


def _heartbeat_envelope(
    detail: ConversationDetail,
    recent: tuple[MessageText, ...],
    now: datetime,
) -> MessageEnvelope:
    latest_inbound = next(
        (message for message in recent if message.direction == "inbound"),
        None,
    )
    sender = latest_inbound.sender if latest_inbound is not None else f"{detail.platform}:unknown"
    topic = latest_inbound.text if latest_inbound is not None else "scheduled heartbeat"
    return MessageEnvelope(
        id=f"heartbeat:{detail.id}:{int(now.timestamp())}",
        connection_id=detail.connection_id,
        platform=Platform(detail.platform),
        chat_kind=ChatKind(detail.chat_kind),
        chat_id=detail.chat_id,
        sender_identity_id=sender,
        occurred_at=now,
        segments=(TextSegment(text=topic),),
    )


def _recent_context(
    detail: ConversationDetail, recent: tuple[MessageText, ...]
) -> str:
    lines = [
        f"Conversation: {detail.stable_key}",
        "Recent messages, oldest first:",
    ]
    lines.extend(
        f"{message.direction}:{message.sender}: {message.text}"
        for message in reversed(recent)
    )
    if not recent:
        lines.append("(no recent text available)")
    return "\n".join(lines)
