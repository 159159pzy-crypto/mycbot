"""Agent turn execution: budget gate, bounded tool loop, fallback, audit."""

import asyncio
import json
from collections.abc import Callable, Sequence
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
from mybot.engine.prompt import HistoryEntry, assemble_messages, envelope_text
from mybot.engine.reply_shaping import shape_reply
from mybot.infrastructure.budget import TokenBudget
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply, ToolCall
from mybot.repositories.messages import MessageText
from mybot.repositories.tool_invocations import ToolInvocationRecord
from mybot.repositories.turns import TurnOutcome
from mybot.tools import ToolCatalog, ToolExecutor, collect_citations

logger = structlog.get_logger("mybot.agent")

PERSONA_KEY = "agent.system_prompt"
NOT_CONFIGURED_FALLBACK = (
    "The LLM agent is not configured yet (set MYBOT_LLM_BASE_URL and MYBOT_LLM_API_KEY), "
    "so this is the built-in fallback reply."
)
LLM_FAILURE_FALLBACK = (
    "Sorry — my language model is unavailable right now. Please try again shortly."
)
BUDGET_FALLBACK = (
    "I have reached today's token budget for this chat, so I am pausing replies "
    "until tomorrow."
)
NON_TEXT_PLACEHOLDER = "[the user sent a non-text message]"
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
    async def retrieval_block(
        self, envelope: MessageEnvelope, inbound_text: str
    ) -> str | None: ...

    async def extract_and_store(
        self, envelope: MessageEnvelope, reply_text: str
    ) -> "ExtractionUsage": ...


class ExtractionUsage(Protocol):
    @property
    def stored(self) -> int: ...

    @property
    def prompt_tokens(self) -> int: ...

    @property
    def completion_tokens(self) -> int: ...


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
    tools: ToolCatalog | None = None
    executor: ToolExecutor | None = None
    granted_capabilities: tuple[str, ...] = ()
    max_tool_calls: int = 5
    turn_deadline_seconds: float = 90.0
    invocations: InvocationStore | None = None
    memory: MemoryHooks | None = None
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
    ) -> ReplyPlan:
        inbound_text = envelope_text(envelope) or NON_TEXT_PLACEHOLDER

        if self.llm is None:
            await self._record(
                conversation_id, decision, "fallback", inbound_message_id
            )
            return shape_reply(NOT_CONFIGURED_FALLBACK, capabilities=capabilities)

        today = self.now().date().isoformat()
        if self.budget is not None and not await self.budget.allows(
            stable_key, today=today
        ):
            await self._record(
                conversation_id, decision, "budget_exceeded", inbound_message_id
            )
            return shape_reply(BUDGET_FALLBACK, capabilities=capabilities)

        messages: list[ChatMessage] = assemble_messages(
            system_prompt=await self._system_prompt(),
            platform=envelope.platform,
            chat_kind=envelope.chat_kind,
            history=await self._history_window(conversation_id, inbound_text),
            inbound_sender=envelope.sender_identity_id,
            inbound_text=inbound_text,
            token_budget=self.history_token_budget,
        )
        memory_block = await self._memory_block(envelope, inbound_text)
        if memory_block is not None:
            messages.insert(1, ChatMessage(role="system", content=memory_block))
        if self.tools is not None:
            try:
                await self.tools.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tool_catalog_refresh_failed")
        state = _ToolLoopState(model=self.llm_model_name)
        started = self.clock()
        try:
            async with asyncio.timeout(self.turn_deadline_seconds):
                final_text = await self._tool_loop(messages, envelope, state)
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
            final_text, capabilities=capabilities, citations=tuple(state.citations)
        )

    async def _tool_loop(
        self,
        messages: list[ChatMessage],
        envelope: MessageEnvelope,
        state: _ToolLoopState,
    ) -> str:
        """Offer tools while budget remains; always end on a plain text reply."""

        while True:
            offer: list[dict[str, object]] | None = None
            if (
                self.tools is not None
                and self.executor is not None
                and state.calls_used < self.max_tool_calls
            ):
                offer = self.tools.openai_tools(self.granted_capabilities) or None
            reply = await self.llm.complete(messages, tools=offer)  # type: ignore[union-attr]
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
                messages.append(await self._execute_call(call, envelope, state))

    async def _execute_call(
        self,
        call: ToolCall,
        envelope: MessageEnvelope,
        state: _ToolLoopState,
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
                granted_capabilities=self.granted_capabilities,
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
        return shape_reply(LLM_FAILURE_FALLBACK, capabilities=capabilities)

    async def _memory_block(
        self, envelope: MessageEnvelope, inbound_text: str
    ) -> str | None:
        if self.memory is None:
            return None
        try:
            return await self.memory.retrieval_block(envelope, inbound_text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory_retrieval_failed")
            return None

    async def after_reply(
        self, *, envelope: MessageEnvelope, stable_key: str, reply_text: str
    ) -> None:
        """Post-turn memory extraction; failures never affect the delivered reply."""

        if self.memory is None or self.llm is None:
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

    async def _system_prompt(self) -> str:
        try:
            override = await self.persona.get(PERSONA_KEY)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent_persona_lookup_failed")
            return self.default_system_prompt
        if isinstance(override, str) and override.strip():
            return override.strip()
        return self.default_system_prompt

    async def _history_window(
        self, conversation_id: UUID, inbound_text: str
    ) -> tuple[HistoryEntry, ...]:
        rows = await self.history.recent_texts(
            conversation_id, limit=self.history_max_messages
        )
        # The just-persisted inbound message is the newest row; keep it out of history.
        if rows and rows[0].direction == "inbound" and rows[0].text == inbound_text:
            rows = rows[1:]
        return tuple(
            HistoryEntry(direction=row.direction, sender=row.sender, text=row.text)
            for row in rows
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
