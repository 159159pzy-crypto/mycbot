import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mybot.adapters import InboundEvent, OutboundMessage
from mybot.contracts import (
    AgentProfile,
    AnnotationMatch,
    ChatKind,
    ConversationKey,
    MessageEnvelope,
    PersonaVersion,
    Platform,
    PlatformCapabilities,
    ReplyPlan,
    TextSegment,
    TurnDecision,
    WillingnessComponents,
    WillingnessScore,
)
from mybot.engine.agent_turns import AgentRuntime
from mybot.infrastructure.health import (
    DependencyHealth,
    DependencyStatus,
    ReadinessDependencies,
    ReadinessResponse,
)
from mybot.infrastructure.streams import (
    MemoryStreamBackend,
    StreamConsumer,
    StreamPublisher,
)
from mybot.repositories.conversations import ConversationRecord
from mybot.repositories.messages import StoredMessage
from mybot.repositories.profiles import ResolvedProfile
from mybot.runtime import ProcessMode
from mybot.services.agent_worker import DEFAULT_CAPABILITIES, AgentWorkerService


@dataclass
class FakeConversations:
    record: ConversationRecord
    calls: list[ConversationKey] = field(default_factory=list)
    ephemeral_calls: list[bool] = field(default_factory=list)

    async def get_or_create(
        self, key: ConversationKey, *, platform: Platform, ephemeral: bool = False
    ) -> ConversationRecord:
        self.calls.append(key)
        self.ephemeral_calls.append(ephemeral)
        return self.record


@dataclass
class FakeMessages:
    duplicate: bool = False
    own_ids: tuple[str, ...] = ()
    inbound: list[MessageEnvelope] = field(default_factory=list)
    outbound: list[ReplyPlan] = field(default_factory=list)

    async def record_inbound(
        self, conversation_id: UUID, envelope: MessageEnvelope
    ) -> StoredMessage:
        self.inbound.append(envelope)
        return StoredMessage(id=uuid4(), duplicate=self.duplicate)

    async def record_outbound(
        self,
        conversation_id: UUID,
        plan: ReplyPlan,
        *,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID:
        self.outbound.append(plan)
        return uuid4()

    async def recent_outbound_platform_ids(
        self, conversation_id: UUID, *, limit: int = 50
    ) -> tuple[str, ...]:
        return self.own_ids


class FakeReadiness:
    async def check(self) -> ReadinessResponse:
        return ReadinessResponse(
            status="ready",
            dependencies=ReadinessDependencies(
                database=DependencyHealth(status=DependencyStatus.UP),
                redis=DependencyHealth(status=DependencyStatus.UP),
            ),
        )


@dataclass
class FakeAgentEngine:
    delay_seconds: float = 0.0
    calls: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    after_replies: list[tuple[str, str, str]] = field(default_factory=list)
    runtimes: list[AgentRuntime | None] = field(default_factory=list)

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
        self.runtimes.append(runtime)
        self.calls.append(envelope.id)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.completed.append(envelope.id)
        return ReplyPlan(text_segments=(f"agent-reply:{envelope.id}",))

    async def after_reply(
        self,
        *,
        envelope: MessageEnvelope,
        stable_key: str,
        reply_text: str,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self.after_replies.append((envelope.id, stable_key, reply_text))


@dataclass
class FailOnceAgent(FakeAgentEngine):
    async def run_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        if not self.calls:
            self.calls.append(kwargs["envelope"].id)
            raise RuntimeError("transient model failure")
        return await super().run_turn(**kwargs)


@dataclass
class RetryMessages(FakeMessages):
    stored_id: UUID = field(default_factory=uuid4)
    inbound_calls: int = 0

    async def record_inbound(
        self, conversation_id: UUID, envelope: MessageEnvelope
    ) -> StoredMessage:
        self.inbound_calls += 1
        self.inbound.append(envelope)
        return StoredMessage(id=self.stored_id, duplicate=self.inbound_calls > 1)


@dataclass
class FakeProfiles:
    resolved: ResolvedProfile

    async def resolve(self, conversation_id, **kwargs):  # type: ignore[no-untyped-def]
        return self.resolved


@dataclass
class FakeWillingness:
    allowed: bool
    calls: int = 0

    async def evaluate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        score = 0.82 if self.allowed else 0.2
        return WillingnessScore(
            score=score,
            threshold=0.7,
            allowed=self.allowed,
            reason="test relevance",
            components=WillingnessComponents(),
        )


@dataclass
class FakeEventSink:
    posted: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def post_event(self, kind: str, payload: dict[str, object]) -> None:  # type: ignore[override]
        self.posted.append((kind, payload))


@dataclass
class FakeMemoryCommands:
    revoked_count: int = 3
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def forget(
        self, *, subject_identity_id: str, conversation_stable_key: str | None
    ) -> int:
        self.calls.append((subject_identity_id, conversation_stable_key))
        return self.revoked_count


@dataclass
class FakeAnnotations:
    answer: str
    calls: int = 0

    async def match(self, query: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return AnnotationMatch(
            annotation_id=uuid4(), answer=self.answer, score=0.98, threshold=0.92
        )


def envelope(
    *,
    text: str = "hello",
    chat_kind: ChatKind = ChatKind.DIRECT,
    message_id: str = "901",
) -> MessageEnvelope:
    return MessageEnvelope(
        id=f"qq:qq-main:{message_id}",
        connection_id="qq-main",
        platform=Platform.QQ,
        chat_kind=chat_kind,
        chat_id="10001" if chat_kind is ChatKind.DIRECT else "333",
        sender_identity_id="qq:10001",
        occurred_at=datetime(2026, 7, 26, 4, tzinfo=UTC),
        segments=(TextSegment(text=text),),
    )


def build_service(
    backend: MemoryStreamBackend,
    *,
    duplicate: bool = False,
    agent: FakeAgentEngine | None = None,
    memory: FakeMemoryCommands | None = None,
    events: FakeEventSink | None = None,
    profiles: FakeProfiles | None = None,
    willingness: FakeWillingness | None = None,
    annotations: FakeAnnotations | None = None,
) -> tuple[AgentWorkerService, FakeMessages, FakeAgentEngine]:
    conversations = FakeConversations(
        record=ConversationRecord(id=uuid4(), stable_key="v1:qq-main:DIRECT:10001:0")
    )
    messages = FakeMessages(duplicate=duplicate)
    engine = agent or FakeAgentEngine()
    consumer = StreamConsumer(
        backend,
        stream="mybot:ingest",
        group="agent-workers",
        consumer="worker-test",
        dead_letter_stream="mybot:ingest:dead",
        max_attempts=3,
        dedupe_ttl_seconds=60,
        dedupe_prefix="mybot:seen",
        block_ms=10,
        claim_min_idle_ms=5_000,
    )
    service = AgentWorkerService(
        consumer=consumer,
        outbound=StreamPublisher(backend=backend, stream="mybot:outbound", maxlen=100),
        conversations=conversations,
        messages=messages,
        readiness=FakeReadiness(),
        agent=engine,
        capabilities=DEFAULT_CAPABILITIES,
        memory=memory,
        events=events,
        profiles=profiles,
        willingness=willingness,
        annotations=annotations,
    )
    return service, messages, engine


async def outbound_messages(backend: MemoryStreamBackend) -> list[OutboundMessage]:
    return [
        OutboundMessage.model_validate_json(payload)
        for _, payload in await backend.entries("mybot:outbound")
    ]


@pytest.mark.asyncio
async def test_direct_message_flows_through_the_agent_engine() -> None:
    backend = MemoryStreamBackend()
    service, messages, engine = build_service(backend)

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert len(messages.inbound) == 1
    assert len(messages.outbound) == 1
    assert engine.calls == ["qq:qq-main:901"]
    outbound = await outbound_messages(backend)
    assert len(outbound) == 1
    assert outbound[0].platform is Platform.QQ
    assert outbound[0].chat_id == "10001"
    assert outbound[0].reply_to_platform_message_id == "901"
    assert outbound[0].reply_plan.text_segments == ("agent-reply:qq:qq-main:901",)


@pytest.mark.asyncio
async def test_bound_profile_runtime_is_passed_to_the_agent() -> None:
    backend = MemoryStreamBackend()
    profile_id = uuid4()
    persona = PersonaVersion(
        profile_id=profile_id,
        version=1,
        system_prompt="群聊人设",
    )
    resolved = ResolvedProfile(
        profile=AgentProfile(
            id=profile_id,
            name="群聊",
            active_persona_version_id=persona.id,
            model_tier="economy",
            tool_capabilities=("web.search",),
        ),
        persona=persona,
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        updated_at=datetime(2026, 7, 28, tzinfo=UTC),
    )
    service, _, engine = build_service(backend, profiles=FakeProfiles(resolved))

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    runtime = engine.runtimes[0]
    assert runtime is not None
    assert runtime.system_prompt == "群聊人设"
    assert runtime.granted_capabilities == ("web.search",)


@pytest.mark.asyncio
async def test_profile_willingness_can_admit_an_unmentioned_group_message() -> None:
    backend = MemoryStreamBackend()
    profile_id = uuid4()
    persona = PersonaVersion(profile_id=profile_id, version=1, system_prompt="群聊人设")
    resolved = ResolvedProfile(
        profile=AgentProfile(
            id=profile_id,
            name="群聊",
            active_persona_version_id=persona.id,
            willingness={"enabled": True, "threshold": 0.7},
        ),
        persona=persona,
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        updated_at=datetime(2026, 7, 28, tzinfo=UTC),
    )
    scorer = FakeWillingness(allowed=True)
    service, messages, engine = build_service(
        backend,
        profiles=FakeProfiles(resolved),
        willingness=scorer,
    )

    await service.handle_payload(
        InboundEvent(envelope=envelope(chat_kind=ChatKind.GROUP)).model_dump_json()
    )

    assert scorer.calls == 1
    assert len(engine.calls) == 1
    assert len(messages.outbound) == 1


@pytest.mark.asyncio
async def test_duplicate_inbound_message_is_not_answered_twice() -> None:
    backend = MemoryStreamBackend()
    service, messages, _ = build_service(backend, duplicate=True)

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert messages.outbound == []
    assert await outbound_messages(backend) == []


@pytest.mark.asyncio
async def test_unaddressed_group_message_is_persisted_but_ignored() -> None:
    backend = MemoryStreamBackend()
    service, messages, _ = build_service(backend)

    await service.handle_payload(
        InboundEvent(envelope=envelope(chat_kind=ChatKind.GROUP)).model_dump_json()
    )

    assert len(messages.inbound) == 1
    assert messages.outbound == []


@pytest.mark.asyncio
async def test_ping_and_status_commands_stay_deterministic_and_bypass_the_agent() -> None:
    backend = MemoryStreamBackend()
    service, _, engine = build_service(backend)

    await service.handle_payload(
        InboundEvent(envelope=envelope(text="/ping", message_id="1")).model_dump_json()
    )
    await service.handle_payload(
        InboundEvent(envelope=envelope(text="/status", message_id="2")).model_dump_json()
    )

    outbound = await outbound_messages(backend)
    assert outbound[0].reply_plan.text_segments == ("pong",)
    assert "状态: ready" in outbound[1].reply_plan.text_segments[0]
    assert engine.calls == []


@pytest.mark.asyncio
async def test_group_reply_to_recent_bot_message_is_answered() -> None:
    backend = MemoryStreamBackend()
    service, messages, _ = build_service(backend)
    messages.own_ids = ("5001",)
    replying = MessageEnvelope.model_validate(
        {
            **envelope(chat_kind=ChatKind.GROUP).model_dump(),
            "reply_to_message_id": "5001",
        }
    )

    await service.handle_payload(InboundEvent(envelope=replying).model_dump_json())

    assert len(messages.outbound) == 1


@pytest.mark.asyncio
async def test_agent_reply_triggers_the_after_reply_hook() -> None:
    backend = MemoryStreamBackend()
    service, _, engine = build_service(backend)

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert len(engine.after_replies) == 1
    envelope_id, stable_key, reply_text = engine.after_replies[0]
    assert envelope_id == "qq:qq-main:901"
    assert stable_key == "v1:qq-main:DIRECT:10001:0"
    assert reply_text.startswith("agent-reply:")


@pytest.mark.asyncio
async def test_turn_events_are_posted_for_plugin_hooks_even_on_ignore() -> None:
    backend = MemoryStreamBackend()
    sink = FakeEventSink()
    service, _, _ = build_service(backend, events=sink)

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())
    await service.handle_payload(
        InboundEvent(
            envelope=envelope(chat_kind=ChatKind.GROUP, message_id="2")
        ).model_dump_json()
    )

    assert len(sink.posted) == 2
    kind, payload = sink.posted[0]
    assert kind == "message"
    envelope_payload = payload["envelope"]
    decision_payload = payload["decision"]
    assert isinstance(envelope_payload, dict)
    assert envelope_payload["id"] == "qq:qq-main:901"
    assert isinstance(decision_payload, dict)
    assert decision_payload["action"] == "AGENT"
    assert sink.posted[1][1]["decision"]["action"] == "IGNORE"  # type: ignore[index]


@pytest.mark.asyncio
async def test_duplicate_messages_do_not_post_events() -> None:
    backend = MemoryStreamBackend()
    sink = FakeEventSink()
    service, _, _ = build_service(backend, duplicate=True, events=sink)

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert sink.posted == []


@pytest.mark.asyncio
async def test_forget_command_revokes_and_reports_the_count() -> None:
    backend = MemoryStreamBackend()
    memory = FakeMemoryCommands(revoked_count=3)
    service, _, engine = build_service(backend, memory=memory)

    await service.handle_payload(
        InboundEvent(envelope=envelope(text="/forget")).model_dump_json()
    )

    outbound = await outbound_messages(backend)
    assert "已撤销 3 条" in outbound[0].reply_plan.text_segments[0]
    assert engine.calls == []  # deterministic path, never the agent
    assert engine.after_replies == []
    subject, conversation_scope = memory.calls[0]
    assert subject == "qq:10001"
    assert conversation_scope == "v1:qq-main:DIRECT:10001:0"  # DM also clears the chat


@pytest.mark.asyncio
async def test_group_forget_only_revokes_subject_memories() -> None:
    backend = MemoryStreamBackend()
    memory = FakeMemoryCommands()
    service, _, _ = build_service(backend, memory=memory)

    await service.handle_payload(
        InboundEvent(
            envelope=envelope(text="/forget", chat_kind=ChatKind.GROUP)
        ).model_dump_json()
    )

    assert memory.calls[0][1] is None


@pytest.mark.asyncio
async def test_forget_without_memory_reports_disabled() -> None:
    backend = MemoryStreamBackend()
    service, _, _ = build_service(backend, memory=None)

    await service.handle_payload(
        InboundEvent(envelope=envelope(text="/forget")).model_dump_json()
    )

    outbound = await outbound_messages(backend)
    assert "未启用记忆" in outbound[0].reply_plan.text_segments[0]


@dataclass
class FakeGuards:
    kind: str = "allow"
    refusal_text: str = ""

    async def check(self, event, decision, *, conversation_id, stable_key):  # type: ignore[no-untyped-def]
        @dataclass
        class Verdict:
            kind: str
            reason: str = "scripted"
            refusal_text: str = ""

        return Verdict(kind=self.kind, refusal_text=self.refusal_text)


@dataclass
class RejectingModeration:
    async def allows(self, text: str) -> bool:
        return "禁词" not in text


@pytest.mark.asyncio
async def test_guard_refusal_replies_deterministically_without_the_agent() -> None:
    backend = MemoryStreamBackend()
    service, messages, engine = build_service(backend)
    service.guards = FakeGuards(kind="refuse", refusal_text="slow down please")

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    outbound = await outbound_messages(backend)
    assert outbound[0].reply_plan.text_segments == ("slow down please",)
    assert engine.calls == []
    assert engine.after_replies == []
    assert len(messages.outbound) == 1


@pytest.mark.asyncio
async def test_guard_ignore_persists_but_stays_silent() -> None:
    backend = MemoryStreamBackend()
    service, messages, engine = build_service(backend)
    service.guards = FakeGuards(kind="ignore")

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert len(messages.inbound) == 1
    assert messages.outbound == []
    assert await outbound_messages(backend) == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_moderation_hook_withholds_rejected_replies() -> None:
    from mybot.services.agent_worker import MODERATION_NOTICE

    backend = MemoryStreamBackend()

    @dataclass
    class BannedAgent(FakeAgentEngine):
        async def run_turn(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(kwargs["envelope"].id)
            self.completed.append(kwargs["envelope"].id)
            return ReplyPlan(text_segments=("这里有禁词",))

    service, messages, _ = build_service(backend, agent=BannedAgent())
    service.moderation = RejectingModeration()

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    outbound = await outbound_messages(backend)
    assert outbound[0].reply_plan.text_segments == (MODERATION_NOTICE,)
    assert messages.outbound[0].text_segments == (MODERATION_NOTICE,)


@pytest.mark.asyncio
async def test_annotation_reply_skips_chat_model_but_still_runs_moderation() -> None:
    from mybot.services.agent_worker import MODERATION_NOTICE

    backend = MemoryStreamBackend()
    annotations = FakeAnnotations(answer="这里有禁词")
    service, messages, engine = build_service(backend, annotations=annotations)
    service.moderation = RejectingModeration()

    await service.handle_payload(InboundEvent(envelope=envelope()).model_dump_json())

    assert annotations.calls == 1
    assert engine.calls == []
    assert engine.after_replies == []
    assert messages.outbound[0].text_segments == (MODERATION_NOTICE,)


@pytest.mark.asyncio
async def test_turns_serialize_per_conversation_but_not_across_conversations() -> None:
    backend = MemoryStreamBackend()
    engine = FakeAgentEngine(delay_seconds=0.05)
    service, _, _ = build_service(backend, agent=engine)

    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        service.handle_payload(InboundEvent(envelope=envelope(message_id="1")).model_dump_json()),
        service.handle_payload(InboundEvent(envelope=envelope(message_id="2")).model_dump_json()),
    )
    serialized_elapsed = asyncio.get_running_loop().time() - started

    assert engine.completed[:2] == ["qq:qq-main:1", "qq:qq-main:2"]
    assert serialized_elapsed >= 0.1  # same conversation: strict sequence

    group_mention = InboundEvent(
        envelope=envelope(chat_kind=ChatKind.GROUP, message_id="3"),
        mentions_self=True,
    ).model_dump_json()
    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        service.handle_payload(InboundEvent(envelope=envelope(message_id="4")).model_dump_json()),
        service.handle_payload(group_mention),
    )
    concurrent_elapsed = asyncio.get_running_loop().time() - started

    assert concurrent_elapsed < 0.1  # different conversations overlap
    assert len(engine.completed) == 4


@pytest.mark.asyncio
async def test_malformed_payload_dead_letters_and_acks() -> None:
    backend = MemoryStreamBackend()
    service, messages, _consumer = build_service(backend)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)

    await publisher.publish("not-json")
    stop_event = asyncio.Event()
    task = asyncio.create_task(service.run(ProcessMode.AGENT_WORKER, stop_event))
    for _ in range(200):
        if [payload for _, payload in await backend.entries("mybot:ingest:dead")]:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    dead = [payload for _, payload in await backend.entries("mybot:ingest:dead")]
    assert dead == ["not-json"]
    assert messages.inbound == []
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_lifecycle_processes_stream_and_stops_cleanly() -> None:
    backend = MemoryStreamBackend()
    service, messages, _ = build_service(backend)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    stop_event = asyncio.Event()

    task = asyncio.create_task(service.run(ProcessMode.AGENT_WORKER, stop_event))
    await publisher.publish(InboundEvent(envelope=envelope()).model_dump_json())
    for _ in range(200):
        if messages.outbound:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(messages.outbound) == 1
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_stream_redelivery_resumes_after_inbound_was_already_persisted() -> None:
    class Clock:
        now = 1_000.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    service, _, _ = build_service(backend, agent=FailOnceAgent())
    consumer = service.consumer
    messages = RetryMessages()
    service.messages = messages
    payload = InboundEvent(envelope=envelope()).model_dump_json()
    await StreamPublisher(backend, "mybot:ingest", 100).publish(payload)

    await consumer.process_available(service.handle_payload, dedupe_key=lambda _: "envelope")
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 1

    clock.now += 10.0
    await consumer.process_available(service.handle_payload, dedupe_key=lambda _: "envelope")

    assert messages.inbound_calls == 2
    assert len(messages.outbound) == 1
    assert len(await outbound_messages(backend)) == 1
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0
