"""Agent turn execution: budget gate, bounded tool loop, fallback, audit."""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from mybot.contracts import (
    Citation,
    ConversationKey,
    MessageEnvelope,
    PlatformCapabilities,
    ReplyPlan,
    ToolContext,
    TurnDecision,
)
from mybot.contracts.json import thaw_json_object
from mybot.engine.prompt import (
    EnvelopePrompt,
    HistoryEntry,
    assemble_messages,
    envelope_prompt,
    partition_history,
)
from mybot.engine.reply_shaping import shape_reply
from mybot.infrastructure.budget import TokenBudget
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply, ToolCall
from mybot.repositories.messages import MessageText
from mybot.repositories.tool_invocations import ToolInvocationRecord
from mybot.repositories.traces import TraceStatus
from mybot.repositories.turns import TurnOutcome
from mybot.tools import ToolCatalog, ToolExecutor, collect_citations

logger = structlog.get_logger("mybot.agent")

PERSONA_KEY = "agent.system_prompt"
NOT_CONFIGURED_FALLBACK = "语言模型尚未配置, 请管理员先完成模型渠道设置。"
LLM_FAILURE_FALLBACK = "语言模型暂时不可用, 请稍后再试。"
BUDGET_FALLBACK = "本会话今天的 token 预算已用完, 明天再继续聊吧。"
EMPTY_REPLY_FALLBACK = "抱歉, 这次没能组织好回复, 请稍后再试。"
_TOOL_RESULT_CHAR_CAP = 4_000


class LlmCompleter(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...


class PersonaSource(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


class HistorySource(Protocol):
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


class InvocationStore(Protocol):
    async def record_many(
        self, turn_id: UUID, invocations: Sequence[ToolInvocationRecord]
    ) -> None: ...


class MemoryHooks(Protocol):
    async def core_block(self, envelope: MessageEnvelope) -> str | None: ...

    async def retrieval_block(
        self,
        envelope: MessageEnvelope,
        inbound_text: str,
        *,
        limit: int | None = None,
    ) -> str | None: ...

    async def extract_and_store(
        self, envelope: MessageEnvelope, reply_text: str
    ) -> "ExtractionUsage": ...

    async def flush_history(
        self, envelope: MessageEnvelope, history: Sequence[HistoryEntry]
    ) -> "ExtractionUsage": ...

    async def personality_block(
        self,
        envelope: MessageEnvelope,
        *,
        expression_examples: int,
        relationship_enabled: bool,
    ) -> str | None: ...


class ExtractionUsage(Protocol):
    @property
    def stored(self) -> int: ...

    @property
    def prompt_tokens(self) -> int: ...

    @property
    def completion_tokens(self) -> int: ...


class VisionPreparer(Protocol):
    async def prepare(self, envelope: MessageEnvelope) -> EnvelopePrompt: ...


class SkillPromptSource(Protocol):
    def prompt_catalog(self, *, max_chars: int) -> str: ...


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


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class _ToolLoopState:
    """Mutable accumulation across one turn's tool loop."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    calls_used: int = 0
    invocations: list[ToolInvocationRecord] = field(
        default_factory=list[ToolInvocationRecord]
    )
    citations: list[Citation] = field(default_factory=list[Citation])


@dataclass(slots=True, frozen=True)
class AgentRuntime:
    system_prompt: str | None = None
    granted_capabilities: tuple[str, ...] | None = None
    memory_enabled: bool = True
    memory_retrieval_limit: int | None = None
    expression_examples: int = 0
    relationship_enabled: bool = True


@dataclass(slots=True)
class AgentTurnEngine:
    """Runs one AGENT turn end to end; every outcome leaves a turn record."""

    llm: LlmCompleter | None
    persona: PersonaSource
    history: HistorySource
    turns: TurnStore
    budget: TokenBudget | None
    default_system_prompt: str
    llm_model_name: str = "unconfigured"
    history_max_messages: int = 40
    history_token_budget: int = 6_000
    history_flush_max_messages: int = 24
    tools: ToolCatalog | None = None
    executor: ToolExecutor | None = None
    granted_capabilities: tuple[str, ...] = ()
    max_tool_calls: int = 5
    turn_deadline_seconds: float = 90.0
    invocations: InvocationStore | None = None
    memory: MemoryHooks | None = None
    vision: VisionPreparer | None = None
    vision_llm: LlmCompleter | None = None
    skills: SkillPromptSource | None = None
    skill_prompt_max_chars: int = 4_000
    traces: TraceSink | None = None
    not_configured_fallback: str = NOT_CONFIGURED_FALLBACK
    llm_failure_fallback: str = LLM_FAILURE_FALLBACK
    budget_fallback: str = BUDGET_FALLBACK
    empty_reply_fallback: str = EMPTY_REPLY_FALLBACK
    now: Callable[[], datetime] = field(default=_utc_now)
    clock: Callable[[], float] = field(default=monotonic)

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
    ) -> ReplyPlan:
        vision_started = self.clock()
        prepared = await self._prepare_inbound(envelope)
        turn_llm = (
            self.vision_llm
            if not isinstance(prepared.content, str) and self.vision_llm is not None
            else self.llm
        )
        await self._trace(
            envelope,
            conversation_id,
            "vision.prepare",
            duration_ms=int((self.clock() - vision_started) * 1_000),
            attributes={"mode": "multimodal" if not isinstance(prepared.content, str) else "text"},
        )
        inbound_text = prepared.summary

        if turn_llm is None:
            await self._record(
                conversation_id, decision, "fallback", inbound_message_id
            )
            return shape_reply(
                self.not_configured_fallback,
                capabilities=capabilities,
                empty_fallback=self.empty_reply_fallback,
            )

        today = self.now().date().isoformat()
        if self.budget is not None and not await self.budget.allows(
            stable_key, today=today
        ):
            await self._record(
                conversation_id, decision, "budget_exceeded", inbound_message_id
            )
            return shape_reply(
                self.budget_fallback,
                capabilities=capabilities,
                empty_fallback=self.empty_reply_fallback,
            )

        system_prompt = await self._system_prompt(
            runtime.system_prompt if runtime is not None else None
        )
        history = await self._history_window(
            conversation_id,
            inbound_text,
            system_prompt=system_prompt,
            envelope=envelope,
            stable_key=stable_key,
        )
        messages: list[ChatMessage] = assemble_messages(
            system_prompt=system_prompt,
            platform=envelope.platform,
            chat_kind=envelope.chat_kind,
            history=history,
            inbound_sender=envelope.sender_identity_id,
            inbound_text=inbound_text,
            token_budget=self.history_token_budget,
            inbound_content=prepared.content,
        )
        memory_enabled = runtime is None or runtime.memory_enabled
        memory_block = (
            None
            if envelope.ephemeral or not memory_enabled
            else await self._memory_block(
                envelope,
                inbound_text,
                conversation_id,
                retrieval_limit=(
                    runtime.memory_retrieval_limit if runtime is not None else None
                ),
            )
        )
        core_block = (
            None
            if envelope.ephemeral or not memory_enabled
            else await self._core_memory_block(envelope, conversation_id)
        )
        personality_block = (
            None
            if (
                envelope.ephemeral
                or not memory_enabled
                or runtime is None
                or (runtime.expression_examples <= 0 and not runtime.relationship_enabled)
            )
            else await self._personality_memory_block(
                envelope,
                conversation_id,
                expression_examples=runtime.expression_examples,
                relationship_enabled=runtime.relationship_enabled,
            )
        )
        if memory_block is not None:
            messages.insert(1, ChatMessage(role="system", content=memory_block))
        if personality_block is not None:
            messages.insert(1, ChatMessage(role="system", content=personality_block))
        if core_block is not None:
            messages.insert(1, ChatMessage(role="system", content=core_block))
        if self.tools is not None:
            try:
                await self.tools.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tool_catalog_refresh_failed")
        state = _ToolLoopState(model=self.llm_model_name)
        granted_capabilities = (
            self.granted_capabilities
            if runtime is None or runtime.granted_capabilities is None
            else runtime.granted_capabilities
        )
        started = self.clock()
        try:
            async with asyncio.timeout(self.turn_deadline_seconds):
                final_text = await self._tool_loop(
                    messages,
                    envelope,
                    state,
                    conversation_id,
                    granted_capabilities,
                    turn_llm,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await self._finish_error(
                conversation_id,
                decision,
                inbound_message_id,
                state,
                started,
                capabilities,
                error="turn_deadline_exceeded",
            )
        except LlmError as error:
            logger.warning("agent_llm_failed", error=str(error))
            return await self._finish_error(
                conversation_id,
                decision,
                inbound_message_id,
                state,
                started,
                capabilities,
                error=str(error),
            )
        latency_ms = int((self.clock() - started) * 1000)
        turn_id = await self._record(
            conversation_id,
            decision,
            "replied",
            inbound_message_id,
            model=state.model,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            latency_ms=latency_ms,
        )
        await self._record_invocations(turn_id, state)
        if self.budget is not None:
            await self.budget.consume(
                stable_key,
                state.prompt_tokens + state.completion_tokens,
                today=today,
            )
        return shape_reply(
            final_text,
            capabilities=capabilities,
            citations=tuple(state.citations),
            empty_fallback=self.empty_reply_fallback,
        )

    async def _tool_loop(
        self,
        messages: list[ChatMessage],
        envelope: MessageEnvelope,
        state: _ToolLoopState,
        conversation_id: UUID,
        granted_capabilities: tuple[str, ...],
        llm: LlmCompleter,
    ) -> str:
        """Offer tools while budget remains; always end on a plain text reply."""

        while True:
            offer: list[dict[str, object]] | None = None
            if (
                self.tools is not None
                and self.executor is not None
                and state.calls_used < self.max_tool_calls
            ):
                offer = self.tools.openai_tools(granted_capabilities) or None
            call_started = self.clock()
            try:
                reply = await llm.complete(messages, tools=offer)
            except LlmError as error:
                await self._trace(
                    envelope,
                    conversation_id,
                    "llm.complete",
                    status="error",
                    duration_ms=int((self.clock() - call_started) * 1_000),
                    attributes={
                        "error_code": type(error).__name__,
                        "retryable": error.retryable,
                    },
                )
                raise
            await self._trace(
                envelope,
                conversation_id,
                "llm.complete",
                duration_ms=int((self.clock() - call_started) * 1_000),
                attributes={
                    "model": reply.model,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "tool_calls": len(reply.tool_calls),
                },
            )
            state.prompt_tokens += reply.prompt_tokens
            state.completion_tokens += reply.completion_tokens
            state.model = reply.model
            if not reply.tool_calls or self.executor is None:
                return reply.text
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=reply.text or None,
                    tool_calls=reply.tool_calls,
                )
            )
            for call in reply.tool_calls:
                messages.append(
                    await self._execute_call(
                        call,
                        envelope,
                        state,
                        conversation_id,
                        granted_capabilities,
                    )
                )

    async def _execute_call(
        self,
        call: ToolCall,
        envelope: MessageEnvelope,
        state: _ToolLoopState,
        conversation_id: UUID,
        granted_capabilities: tuple[str, ...],
    ) -> ChatMessage:
        arguments, argument_error = _parse_arguments(call.arguments)
        call_started = self.clock()
        if argument_error is not None:
            payload: dict[str, JsonValue] = {
                "error": "invalid_arguments",
                "message": argument_error,
            }
            ok = False
            error_code: str | None = "invalid_arguments"
        elif state.calls_used >= self.max_tool_calls:
            payload = {
                "error": "tool_limit",
                "message": "the per-turn tool call limit was reached; answer now",
            }
            ok = False
            error_code = "tool_limit"
        else:
            assert self.executor is not None
            context = ToolContext(
                invocation_id=uuid4(),
                conversation=ConversationKey(
                    connection_id=envelope.connection_id,
                    chat_kind=envelope.chat_kind,
                    chat_id=envelope.chat_id,
                    thread_id=envelope.thread_id,
                ),
                actor_identity_id=envelope.sender_identity_id,
                granted_capabilities=granted_capabilities,
                correlation_id=envelope.id,
            )
            result = await self.executor.execute(call.name, context, arguments or {})
            ok = result.ok
            if result.ok and result.data is not None:
                state.citations.extend(
                    citation
                    for citation in collect_citations(result)
                    if citation.uri not in {existing.uri for existing in state.citations}
                )
                payload = dict(thaw_json_object(result.data))
                error_code = None
            else:
                error_code = result.error.code if result.error is not None else "tool_error"
                payload = {
                    "error": error_code,
                    "message": result.error.message if result.error is not None else "",
                }
        state.calls_used += 1
        latency_ms = int((self.clock() - call_started) * 1000)
        state.invocations.append(
            ToolInvocationRecord(
                tool_id=call.name,
                ok=ok,
                error_code=error_code,
                latency_ms=latency_ms,
            )
        )
        logger.info(
            "tool_invoked",
            tool_id=call.name,
            ok=ok,
            error_code=error_code,
            latency_ms=latency_ms,
        )
        await self._trace(
            envelope,
            conversation_id,
            "tool.call",
            status="ok" if ok else "error",
            duration_ms=latency_ms,
            attributes={
                "tool_id": call.name,
                "ok": ok,
                "error_code": error_code,
            },
        )
        content = json.dumps(payload, ensure_ascii=False, default=str)
        if len(content) > _TOOL_RESULT_CHAR_CAP:
            content = content[: _TOOL_RESULT_CHAR_CAP - 1] + "…"
        return ChatMessage(
            role="tool", content=content, tool_call_id=call.id, name=call.name
        )

    async def _finish_error(
        self,
        conversation_id: UUID,
        decision: TurnDecision,
        inbound_message_id: UUID | None,
        state: _ToolLoopState,
        started: float,
        capabilities: PlatformCapabilities,
        *,
        error: str,
    ) -> ReplyPlan:
        latency_ms = int((self.clock() - started) * 1000)
        turn_id = await self._record(
            conversation_id,
            decision,
            "error",
            inbound_message_id,
            model=state.model,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            latency_ms=latency_ms,
            error=error,
        )
        await self._record_invocations(turn_id, state)
        return shape_reply(
            self.llm_failure_fallback,
            capabilities=capabilities,
            empty_fallback=self.empty_reply_fallback,
        )

    async def _memory_block(
        self,
        envelope: MessageEnvelope,
        inbound_text: str,
        conversation_id: UUID,
        *,
        retrieval_limit: int | None = None,
    ) -> str | None:
        if self.memory is None:
            await self._trace(
                envelope,
                conversation_id,
                "memory.retrieval",
                status="skipped",
                attributes={"reason": "disabled"},
            )
            return None
        started = self.clock()
        try:
            block = (
                await self.memory.retrieval_block(
                    envelope, inbound_text, limit=retrieval_limit
                )
                if retrieval_limit is not None
                else await self.memory.retrieval_block(envelope, inbound_text)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory_retrieval_failed")
            await self._trace(
                envelope,
                conversation_id,
                "memory.retrieval",
                status="error",
                duration_ms=int((self.clock() - started) * 1_000),
                attributes={"error_code": "memory_retrieval_failed"},
            )
            return None
        await self._trace(
            envelope,
            conversation_id,
            "memory.retrieval",
            duration_ms=int((self.clock() - started) * 1_000),
            attributes={
                "recalled": block is not None,
                "summary": (block or "")[:2_000],
            },
        )
        return block

    async def _core_memory_block(
        self, envelope: MessageEnvelope, conversation_id: UUID
    ) -> str | None:
        if self.memory is None:
            return None
        loader = getattr(self.memory, "core_block", None)
        if loader is None:
            return None
        started = self.clock()
        try:
            block = await loader(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("core_memory_lookup_failed")
            await self._trace(
                envelope,
                conversation_id,
                "memory.core",
                status="error",
                duration_ms=int((self.clock() - started) * 1_000),
                attributes={"error_code": "core_memory_lookup_failed"},
            )
            return None
        await self._trace(
            envelope,
            conversation_id,
            "memory.core",
            duration_ms=int((self.clock() - started) * 1_000),
            attributes={"loaded": block is not None},
        )
        return block

    async def _personality_memory_block(
        self,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        *,
        expression_examples: int,
        relationship_enabled: bool,
    ) -> str | None:
        if self.memory is None:
            return None
        loader = getattr(self.memory, "personality_block", None)
        if loader is None:
            return None
        started = self.clock()
        try:
            block = await loader(
                envelope,
                expression_examples=expression_examples,
                relationship_enabled=relationship_enabled,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("personality_memory_lookup_failed")
            await self._trace(
                envelope,
                conversation_id,
                "memory.personality",
                status="error",
                duration_ms=int((self.clock() - started) * 1_000),
                attributes={"error_code": "personality_memory_lookup_failed"},
            )
            return None
        await self._trace(
            envelope,
            conversation_id,
            "memory.personality",
            duration_ms=int((self.clock() - started) * 1_000),
            attributes={"loaded": block is not None},
        )
        return block

    async def after_reply(
        self,
        *,
        envelope: MessageEnvelope,
        stable_key: str,
        reply_text: str,
        runtime: AgentRuntime | None = None,
    ) -> None:
        """Post-turn memory extraction; failures never affect the delivered reply."""

        if (
            envelope.ephemeral
            or self.memory is None
            or self.llm is None
            or (runtime is not None and not runtime.memory_enabled)
        ):
            return
        try:
            outcome = await self.memory.extract_and_store(envelope, reply_text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory_extraction_failed")
            return
        spent = outcome.prompt_tokens + outcome.completion_tokens
        if self.budget is not None and spent > 0:
            await self.budget.consume(
                stable_key, spent, today=self.now().date().isoformat()
            )

    async def _record_invocations(self, turn_id: UUID | None, state: _ToolLoopState) -> None:
        if turn_id is None or self.invocations is None or not state.invocations:
            return
        try:
            await self.invocations.record_many(turn_id, state.invocations)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tool_invocation_audit_failed")

    async def _system_prompt(self, runtime_prompt: str | None = None) -> str:
        if runtime_prompt is not None and runtime_prompt.strip():
            resolved = runtime_prompt.strip()
        else:
            try:
                override = await self.persona.get(PERSONA_KEY)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent_persona_lookup_failed")
                override = None
            resolved = (
                override.strip()
                if isinstance(override, str) and override.strip()
                else self.default_system_prompt
            )
        if self.skills is None:
            return resolved
        catalog = self.skills.prompt_catalog(max_chars=self.skill_prompt_max_chars)
        return f"{resolved}\n\n{catalog}" if catalog else resolved

    async def _prepare_inbound(self, envelope: MessageEnvelope) -> EnvelopePrompt:
        if self.vision is None:
            return envelope_prompt(envelope, include_images=False)
        try:
            return await self.vision.prepare(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("vision_prepare_unexpected_failure")
            return envelope_prompt(envelope, include_images=False)

    async def _history_window(
        self,
        conversation_id: UUID,
        inbound_text: str,
        *,
        system_prompt: str,
        envelope: MessageEnvelope,
        stable_key: str,
    ) -> tuple[HistoryEntry, ...]:
        rows = await self.history.recent_texts(
            conversation_id,
            limit=self.history_max_messages + self.history_flush_max_messages + 1,
        )
        # The just-persisted inbound message is the newest row; keep it out of history.
        if rows and rows[0].direction == "inbound" and rows[0].text == inbound_text:
            rows = rows[1:]
        entries = tuple(
            HistoryEntry(
                direction=row.direction,
                sender=row.sender,
                text=row.text,
                source_id=str(row.id) if row.id is not None else None,
            )
            for row in rows
        )
        partition = partition_history(
            system_prompt=system_prompt,
            platform=envelope.platform,
            chat_kind=envelope.chat_kind,
            history=entries,
            inbound_text=inbound_text,
            token_budget=self.history_token_budget,
            max_messages=self.history_max_messages,
        )
        await self._flush_memory_history(
            envelope,
            conversation_id,
            stable_key,
            partition.dropped,
        )
        return partition.kept

    async def _flush_memory_history(
        self,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        stable_key: str,
        dropped: Sequence[HistoryEntry],
    ) -> None:
        if envelope.ephemeral or self.memory is None or not dropped:
            return
        flusher = getattr(self.memory, "flush_history", None)
        if flusher is None:
            return
        started = self.clock()
        try:
            outcome = await flusher(envelope, dropped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory_flush_failed")
            await self._trace(
                envelope,
                conversation_id,
                "memory.flush",
                status="error",
                duration_ms=int((self.clock() - started) * 1_000),
                attributes={"error_code": "memory_flush_failed"},
            )
            return
        spent = outcome.prompt_tokens + outcome.completion_tokens
        if self.budget is not None and spent > 0:
            await self.budget.consume(
                stable_key,
                spent,
                today=self.now().date().isoformat(),
            )
        await self._trace(
            envelope,
            conversation_id,
            "memory.flush",
            duration_ms=int((self.clock() - started) * 1_000),
            attributes={
                "dropped_messages": len(dropped),
                "stored": outcome.stored,
                "tokens": spent,
            },
        )

    async def _record(
        self,
        conversation_id: UUID,
        decision: TurnDecision,
        outcome: TurnOutcome,
        inbound_message_id: UUID | None,
        *,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> UUID | None:
        try:
            return await self.turns.record_turn(
                conversation_id,
                decision,
                outcome=outcome,
                inbound_message_id=inbound_message_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                error=error,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent_turn_audit_failed", outcome=outcome)
            return None

    async def _trace(
        self,
        envelope: MessageEnvelope,
        conversation_id: UUID,
        stage: str,
        *,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
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
                attributes=attributes,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("trace_span_record_failed", stage=stage)


def _parse_arguments(raw: str) -> tuple[dict[str, JsonValue] | None, str | None]:
    if not raw.strip():
        return {}, None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None, "tool arguments were not valid JSON"
    if not isinstance(decoded, dict):
        return None, "tool arguments must be a JSON object"
    items = cast(dict[object, JsonValue], decoded)
    return {str(key): value for key, value in items.items()}, None
