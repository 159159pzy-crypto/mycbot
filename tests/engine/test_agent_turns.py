import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from mybot.adapters.telegram.translate import TELEGRAM_CAPABILITIES
from mybot.contracts import (
    ChatKind,
    Citation,
    MessageEnvelope,
    Platform,
    TextSegment,
    TurnAction,
    TurnDecision,
    TurnTrigger,
)
from mybot.engine.agent_turns import (
    BUDGET_FALLBACK,
    LLM_FAILURE_FALLBACK,
    NOT_CONFIGURED_FALLBACK,
    AgentTurnEngine,
)
from mybot.infrastructure.budget import TokenBudget
from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply, ToolCall
from mybot.infrastructure.streams import MemoryStreamBackend
from mybot.repositories.messages import MessageText
from mybot.repositories.tool_invocations import ToolInvocationRecord
from mybot.repositories.turns import TurnOutcome
from mybot.tools import ToolExecutor, ToolRegistry


@dataclass
class FakeLlm:
    reply: LlmReply | None = None
    error: LlmError | None = None
    calls: list[list[ChatMessage]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> LlmReply:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        assert self.reply is not None
        return self.reply


@dataclass
class FakePersona:
    values: dict[str, JsonValue] = field(default_factory=dict)

    async def get(self, key: str) -> JsonValue | None:
        return self.values.get(key)


@dataclass
class FakeHistory:
    rows: tuple[MessageText, ...] = ()

    async def recent_texts(
        self, conversation_id: UUID, *, limit: int = 40
    ) -> tuple[MessageText, ...]:
        return self.rows[:limit]


@dataclass
class RecordedTurn:
    outcome: TurnOutcome
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    error: str | None


@dataclass
class FakeTurns:
    recorded: list[RecordedTurn] = field(default_factory=list)

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
    ) -> UUID:
        self.recorded.append(
            RecordedTurn(
                outcome=outcome,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                error=error,
            )
        )
        return uuid4()


def envelope(text: str = "讲个笑话") -> MessageEnvelope:
    return MessageEnvelope(
        id="telegram:telegram-main:777:66",
        connection_id="telegram-main",
        platform=Platform.TELEGRAM,
        chat_kind=ChatKind.DIRECT,
        chat_id="777",
        sender_identity_id="telegram:777",
        occurred_at=datetime(2026, 7, 26, 6, tzinfo=UTC),
        segments=(TextSegment(text=text),),
    )


DECISION = TurnDecision(
    action=TurnAction.AGENT,
    reason="direct chat",
    confidence=1.0,
    trigger=TurnTrigger.DIRECT_MESSAGE,
)


def make_engine(
    *,
    llm: FakeLlm | None,
    turns: FakeTurns,
    budget: TokenBudget | None = None,
    persona: FakePersona | None = None,
    history: FakeHistory | None = None,
) -> AgentTurnEngine:
    return AgentTurnEngine(
        llm=llm,
        persona=persona or FakePersona(),
        history=history or FakeHistory(),
        turns=turns,
        budget=budget,
        default_system_prompt="You are MyBot.",
        llm_model_name="deepseek-chat",
    )


async def run(engine: AgentTurnEngine, env: MessageEnvelope | None = None):  # type: ignore[no-untyped-def]
    return await engine.run_turn(
        conversation_id=uuid4(),
        stable_key="v1:telegram-main:DIRECT:777:0",
        envelope=env or envelope(),
        decision=DECISION,
        inbound_message_id=uuid4(),
        capabilities=TELEGRAM_CAPABILITIES,
    )


@pytest.mark.asyncio
async def test_successful_turn_returns_shaped_reply_and_records_audit_and_spend() -> None:
    backend = MemoryStreamBackend()
    budget = TokenBudget(
        backend=backend, global_daily_ceiling=1_000, conversation_daily_ceiling=500
    )
    llm = FakeLlm(
        reply=LlmReply(
            text="好的,来一个:\n\n程序员为什么分不清万圣节和圣诞节?因为 Oct 31 == Dec 25。",
            model="deepseek-chat-v3",
            prompt_tokens=100,
            completion_tokens=50,
        )
    )
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns, budget=budget)

    plan = await run(engine)

    assert len(plan.text_segments) == 2
    assert "Oct 31" in plan.text_segments[1]
    assert plan.typing.enabled is True
    record = turns.recorded[0]
    assert record.outcome == "replied"
    assert record.model == "deepseek-chat-v3"
    assert (record.prompt_tokens, record.completion_tokens) == (100, 50)
    # spend was consumed: another 400-token turn still fits, but not 500
    assert (
        await budget.allows("v1:telegram-main:DIRECT:777:0", today="2026-07-26") is True
    )
    await budget.consume("v1:telegram-main:DIRECT:777:0", 350, today="2026-07-26")
    assert (
        await budget.allows("v1:telegram-main:DIRECT:777:0", today="2026-07-26") is False
    )


@pytest.mark.asyncio
async def test_llm_failure_returns_fallback_and_records_error() -> None:
    llm = FakeLlm(error=LlmError("HTTP 503 after retries", retryable=True))
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns)

    plan = await run(engine)

    assert plan.text_segments == (LLM_FAILURE_FALLBACK,)
    record = turns.recorded[0]
    assert record.outcome == "error"
    assert record.error is not None and "503" in record.error


@pytest.mark.asyncio
async def test_unconfigured_llm_returns_explicit_fallback() -> None:
    turns = FakeTurns()
    engine = make_engine(llm=None, turns=turns)

    plan = await run(engine)

    assert plan.text_segments == (NOT_CONFIGURED_FALLBACK,)
    assert turns.recorded[0].outcome == "fallback"


@pytest.mark.asyncio
async def test_crossed_ceiling_refuses_without_calling_the_llm() -> None:
    backend = MemoryStreamBackend()
    budget = TokenBudget(
        backend=backend, global_daily_ceiling=100, conversation_daily_ceiling=0
    )
    await budget.consume("other", 100, today=datetime.now(tz=UTC).date().isoformat())
    llm = FakeLlm(reply=LlmReply(text="nope", model="m", prompt_tokens=1, completion_tokens=1))
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns, budget=budget)

    plan = await run(engine)

    assert plan.text_segments == (BUDGET_FALLBACK,)
    assert turns.recorded[0].outcome == "budget_exceeded"
    assert llm.calls == []


@dataclass
class ScriptedLlm:
    replies: list[LlmReply]
    calls: list[tuple[list[ChatMessage], list[dict[str, object]] | None]] = field(
        default_factory=list
    )
    delay_seconds: float = 0.0

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> LlmReply:
        self.calls.append((list(messages), tools))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.replies.pop(0)


@dataclass
class FakeSearchTool:
    from mybot.contracts import ToolSpec as _Spec

    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def spec(self):  # type: ignore[no-untyped-def]
        from mybot.contracts import ToolRisk, ToolSpec

        return ToolSpec.model_validate(
            {
                "id": "web_search",
                "description": "search",
                "input_schema": {"type": "object", "properties": {}},
                "read_only": True,
                "idempotent": True,
                "risk": ToolRisk.LOW,
                "capabilities": ("web.search",),
                "approval_required": False,
            }
        )

    async def run(self, context, arguments):  # type: ignore[no-untyped-def]
        from mybot.contracts import ToolResult

        self.calls.append(dict(arguments))
        return ToolResult.success(
            {
                "results": [{"title": "Weather", "url": "https://a.example/1"}],
                "sources": [{"label": "Weather", "uri": "https://a.example/1"}],
            }
        )


@dataclass
class FakeInvocations:
    recorded: list[tuple[object, list[ToolInvocationRecord]]] = field(default_factory=list)

    async def record_many(self, turn_id, invocations):  # type: ignore[no-untyped-def]
        self.recorded.append((turn_id, list(invocations)))


def tool_call_reply(*, name: str = "web_search", arguments: str = '{"query": "天气"}') -> LlmReply:
    return LlmReply(
        text="",
        model="deepseek-chat-v3",
        prompt_tokens=30,
        completion_tokens=10,
        tool_calls=(ToolCall(id="call_1", name=name, arguments=arguments),),
        finish_reason="tool_calls",
    )


def final_reply(text: str = "今天多云,最高 28 度。") -> LlmReply:
    return LlmReply(
        text=text, model="deepseek-chat-v3", prompt_tokens=50, completion_tokens=20
    )


def make_tool_engine(
    llm: ScriptedLlm,
    turns: FakeTurns,
    *,
    tool: FakeSearchTool | None = None,
    granted: tuple[str, ...] = ("web.search",),
    max_tool_calls: int = 5,
    deadline: float = 30.0,
    invocations: FakeInvocations | None = None,
) -> AgentTurnEngine:
    registry = ToolRegistry([tool or FakeSearchTool()])
    return AgentTurnEngine(
        llm=llm,
        persona=FakePersona(),
        history=FakeHistory(),
        turns=turns,
        budget=None,
        default_system_prompt="You are MyBot.",
        llm_model_name="deepseek-chat",
        tools=registry,
        executor=ToolExecutor(registry, timeout_seconds=5.0),
        granted_capabilities=granted,
        max_tool_calls=max_tool_calls,
        turn_deadline_seconds=deadline,
        invocations=invocations,
    )


@pytest.mark.asyncio
async def test_search_then_answer_produces_cited_reply_and_audit() -> None:
    llm = ScriptedLlm(replies=[tool_call_reply(), final_reply()])
    turns = FakeTurns()
    invocations = FakeInvocations()
    tool = FakeSearchTool()
    engine = make_tool_engine(llm, turns, tool=tool, invocations=invocations)

    plan = await run(engine)

    assert "今天多云" in plan.text_segments[0]
    assert plan.citations == (Citation(label="Weather", uri="https://a.example/1"),)
    assert "Sources:" in plan.text_segments[-1]
    assert tool.calls == [{"query": "天气"}]
    # first call offered tools; the conversation grew by assistant+tool messages
    assert llm.calls[0][1] is not None
    second_messages = llm.calls[1][0]
    assert second_messages[-2].role == "assistant"
    assert second_messages[-1].role == "tool"
    assert second_messages[-1].tool_call_id == "call_1"
    record = turns.recorded[0]
    assert record.outcome == "replied"
    assert record.prompt_tokens == 80  # 30 + 50 across both calls
    assert record.completion_tokens == 30
    assert len(invocations.recorded) == 1
    turn_invocations = invocations.recorded[0][1]
    assert turn_invocations[0].tool_id == "web_search"
    assert turn_invocations[0].ok is True


@pytest.mark.asyncio
async def test_capability_denied_feeds_structured_error_back_to_the_model() -> None:
    llm = ScriptedLlm(replies=[tool_call_reply(), final_reply("无法搜索,但我尽力回答。")])
    turns = FakeTurns()
    invocations = FakeInvocations()
    engine = make_tool_engine(llm, turns, granted=(), invocations=invocations)

    plan = await run(engine)

    assert "尽力回答" in plan.text_segments[0]
    tool_message = llm.calls[1][0][-1]
    assert tool_message.role == "tool"
    assert tool_message.content is not None
    assert "capability_denied" in tool_message.content
    assert invocations.recorded[0][1][0].ok is False
    assert invocations.recorded[0][1][0].error_code == "capability_denied"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_become_structured_error() -> None:
    llm = ScriptedLlm(
        replies=[tool_call_reply(arguments="not-json"), final_reply("好的。")]
    )
    turns = FakeTurns()
    invocations = FakeInvocations()
    tool = FakeSearchTool()
    engine = make_tool_engine(llm, turns, tool=tool, invocations=invocations)

    plan = await run(engine)

    assert plan.text_segments[0] == "好的。"
    assert tool.calls == []  # never executed
    assert invocations.recorded[0][1][0].error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_call_ceiling_stops_offering_tools_and_forces_final_answer() -> None:
    llm = ScriptedLlm(replies=[tool_call_reply(), final_reply("答案。")])
    turns = FakeTurns()
    engine = make_tool_engine(llm, turns, max_tool_calls=1)

    plan = await run(engine)

    assert plan.text_segments[0].startswith("答案。")
    assert llm.calls[0][1] is not None  # first round offered tools
    assert llm.calls[1][1] is None  # ceiling reached: tools withdrawn


@pytest.mark.asyncio
async def test_turn_deadline_yields_fallback_and_error_turn() -> None:
    llm = ScriptedLlm(replies=[final_reply()], delay_seconds=0.3)
    turns = FakeTurns()
    engine = make_tool_engine(llm, turns, deadline=0.05)

    plan = await run(engine)

    assert plan.text_segments == (LLM_FAILURE_FALLBACK,)
    record = turns.recorded[0]
    assert record.outcome == "error"
    assert record.error == "turn_deadline_exceeded"


@dataclass
class FakeMemoryOutcome:
    stored: int = 1
    prompt_tokens: int = 15
    completion_tokens: int = 5


@dataclass
class FakeMemoryHooks:
    block: str | None = "Relevant remembered facts:\n- [subject|2026-07-01] 喜欢美式咖啡"
    fail_retrieval: bool = False
    fail_extraction: bool = False
    extractions: list[str] = field(default_factory=list)

    async def retrieval_block(self, envelope, inbound_text):  # type: ignore[no-untyped-def]
        if self.fail_retrieval:
            raise RuntimeError("retrieval boom")
        return self.block

    async def extract_and_store(self, envelope, reply_text):  # type: ignore[no-untyped-def]
        if self.fail_extraction:
            raise RuntimeError("extraction boom")
        self.extractions.append(reply_text)
        return FakeMemoryOutcome()


@pytest.mark.asyncio
async def test_memory_block_is_injected_as_second_system_message() -> None:
    llm = FakeLlm(reply=LlmReply(text="回答", model="m", prompt_tokens=1, completion_tokens=1))
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns)
    engine.memory = FakeMemoryHooks()

    await run(engine)

    messages = llm.calls[0]
    assert messages[0].role == "system"
    assert messages[1].role == "system"
    assert messages[1].content is not None
    assert "喜欢美式咖啡" in messages[1].content


@pytest.mark.asyncio
async def test_memory_retrieval_failure_leaves_the_prompt_unchanged() -> None:
    llm = FakeLlm(reply=LlmReply(text="回答", model="m", prompt_tokens=1, completion_tokens=1))
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns)
    engine.memory = FakeMemoryHooks(fail_retrieval=True)

    plan = await run(engine)

    assert plan.text_segments[0] == "回答"
    roles = [message.role for message in llm.calls[0]]
    assert roles.count("system") == 1


@pytest.mark.asyncio
async def test_after_reply_extracts_and_consumes_budget() -> None:
    backend = MemoryStreamBackend()
    budget = TokenBudget(backend=backend, global_daily_ceiling=100, conversation_daily_ceiling=0)
    llm = FakeLlm(reply=LlmReply(text="回答", model="m", prompt_tokens=1, completion_tokens=1))
    turns = FakeTurns()
    engine = make_engine(llm=llm, turns=turns, budget=budget)
    hooks = FakeMemoryHooks()
    engine.memory = hooks

    await engine.after_reply(
        envelope=envelope(), stable_key="conv-a", reply_text="记住了你的偏好。"
    )

    assert hooks.extractions == ["记住了你的偏好。"]
    # 15 + 5 tokens consumed against the 100 ceiling; 80 more crosses it
    await budget.consume("conv-a", 80, today=datetime.now(tz=UTC).date().isoformat())
    assert (
        await budget.allows("conv-a", today=datetime.now(tz=UTC).date().isoformat()) is False
    )


@pytest.mark.asyncio
async def test_after_reply_swallows_extraction_failures() -> None:
    llm = FakeLlm(reply=LlmReply(text="回答", model="m", prompt_tokens=1, completion_tokens=1))
    engine = make_engine(llm=llm, turns=FakeTurns())
    engine.memory = FakeMemoryHooks(fail_extraction=True)

    await engine.after_reply(envelope=envelope(), stable_key="conv-a", reply_text="回复")


@pytest.mark.asyncio
async def test_persona_override_and_history_window_shape_the_prompt() -> None:
    llm = FakeLlm(reply=LlmReply(text="回答", model="m", prompt_tokens=1, completion_tokens=1))
    turns = FakeTurns()
    persona = FakePersona(values={"agent.system_prompt": "你是傲娇的猫娘助手。"})
    history = FakeHistory(
        rows=(
            MessageText(direction="inbound", sender="telegram:777", text="讲个笑话"),
            MessageText(direction="outbound", sender="self", text="之前的回答"),
            MessageText(direction="inbound", sender="telegram:777", text="早些的问题"),
        )
    )
    engine = make_engine(llm=llm, turns=turns, persona=persona, history=history)

    await run(engine)

    messages = llm.calls[0]
    assert "你是傲娇的猫娘助手。" in messages[0].content
    contents = [message.content for message in messages]
    # the just-persisted inbound row is excluded; older history is oldest-first
    assert contents[1] == "早些的问题"
    assert contents[2] == "之前的回答"
    assert contents[-1] == "讲个笑话"
    assert contents.count("讲个笑话") == 1
