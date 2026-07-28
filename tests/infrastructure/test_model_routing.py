from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from mybot.infrastructure.llm import ChatMessage, LlmError, LlmReply
from mybot.infrastructure.model_routing import (
    MemoryModelCooldowns,
    ModelCallAttempt,
    ModelChannel,
    ModelPurpose,
    ModelRouter,
    reset_model_profile,
    set_model_profile,
)


def channel(
    name: str,
    *,
    priority: int = 0,
    weight: int = 1,
    api_key_env: str = "TEST_MODEL_KEY",
    chat_model: str = "chat-model",
    tier: str = "default",
) -> dict[str, object]:
    return {
        "name": name,
        "base_url": f"https://{name}.example/v1",
        "api_key_env": api_key_env,
        "priority": priority,
        "weight": weight,
        "tier": tier,
        "model_map": {
            "chat": {
                "model": chat_model,
                "input_price_per_million": "0.50",
                "output_price_per_million": "1.50",
            },
            "memory": {"model": f"{name}-memory"},
            "embedding": {
                "model": f"{name}-embedding",
                "input_price_per_million": "0.02",
            },
        },
    }


class ConfigSource:
    def __init__(self, value: JsonValue | None) -> None:
        self.value = value
        self.calls = 0

    async def get(self, key: str) -> JsonValue | None:
        assert key == "models.channels"
        self.calls += 1
        return self.value


@dataclass
class FakeClient:
    name: str
    model: str
    outcomes: list[object]
    calls: int = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return LlmReply(
            text=f"{self.name}:{outcome}",
            model=self.model,
            prompt_tokens=100,
            completion_tokens=20,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


@dataclass
class AttemptSink:
    attempts: list[ModelCallAttempt] = field(default_factory=list)

    async def record(self, attempt: ModelCallAttempt) -> None:
        self.attempts.append(attempt)


class Factory:
    def __init__(self, outcomes: Mapping[str, list[object]] | None = None) -> None:
        self.outcomes = {name: list(values) for name, values in (outcomes or {}).items()}
        self.created: list[tuple[str, str, str | None]] = []
        self.clients: list[FakeClient] = []

    def __call__(
        self, spec: ModelChannel, purpose: ModelPurpose, api_key: str | None
    ) -> FakeClient:
        target = spec.model_map[purpose]
        self.created.append((spec.name, target.model, api_key))
        client = FakeClient(spec.name, target.model, self.outcomes.get(spec.name, []))
        self.clients.append(client)
        return client


def make_router(
    channels: list[dict[str, object]],
    *,
    outcomes: Mapping[str, list[object]] | None = None,
    clock=lambda: 0.0,  # type: ignore[no-untyped-def]
) -> tuple[ModelRouter, Factory, AttemptSink, ConfigSource, MemoryModelCooldowns]:
    source = ConfigSource(channels)  # type: ignore[arg-type]
    factory = Factory(outcomes)
    sink = AttemptSink()
    cooldowns = MemoryModelCooldowns(clock=clock)
    router = ModelRouter(
        config=source,
        fallback_channels=(),
        cooldowns=cooldowns,
        attempts=sink,
        client_factory=factory,
        secret_lookup=lambda name: "secret-value" if name == "TEST_MODEL_KEY" else None,
        cache_ttl_seconds=30.0,
        cooldown_seconds=60,
        clock=clock,
    )
    return router, factory, sink, source, cooldowns


def test_channel_requires_an_environment_reference_not_a_secret_value() -> None:
    with pytest.raises(ValidationError, match="environment variable"):
        ModelChannel.model_validate(
            channel("primary", api_key_env="sk-this-is-a-secret-not-an-env-name")
        )


@pytest.mark.asyncio
async def test_router_selects_the_purpose_model_and_resolves_secret_at_call_time() -> None:
    router, factory, sink, _, _ = make_router([channel("primary")])

    reply = await router.complete(
        ModelPurpose.MEMORY,
        [ChatMessage(role="user", content="remember this")],
        conversation_id="conversation-1",
    )

    assert reply.model == "primary-memory"
    assert factory.created == [("primary", "primary-memory", "secret-value")]
    assert sink.attempts[0].purpose is ModelPurpose.MEMORY
    assert sink.attempts[0].conversation_id == "conversation-1"


@pytest.mark.asyncio
async def test_embedding_batch_reports_the_model_selected_by_runtime_routing() -> None:
    router, factory, sink, _, _ = make_router([channel("primary")])

    batch = await router.embeddings().embed_with_model(["hello"])

    assert batch.model == "primary-embedding"
    assert batch.vectors == [[0.0, 1.0]]
    assert factory.created == [("primary", "primary-embedding", "secret-value")]
    assert sink.attempts[0].model == "primary-embedding"


@pytest.mark.asyncio
async def test_retryable_failure_cools_channel_and_fails_over() -> None:
    router, factory, sink, _, cooldowns = make_router(
        [channel("primary"), channel("backup", priority=1)],
        outcomes={
            "primary": [LlmError("HTTP 503 response body must not leak", retryable=True)],
            "backup": ["backup-ok"],
        },
    )

    reply = await router.complete(ModelPurpose.CHAT, [ChatMessage(role="user", content="hello")])

    assert reply.text == "backup:backup-ok"
    assert await cooldowns.is_cooling("primary", ModelPurpose.CHAT)
    assert [attempt.status for attempt in sink.attempts] == ["retryable_error", "success"]
    assert sink.attempts[0].error_code == "LlmError"
    assert "response body" not in repr(sink.attempts[0])
    assert [created[0] for created in factory.created] == ["primary", "backup"]


@pytest.mark.asyncio
async def test_permanent_failure_stops_without_trying_later_channels() -> None:
    router, factory, sink, _, _ = make_router(
        [channel("primary"), channel("backup", priority=1)],
        outcomes={"primary": [LlmError("HTTP 401", retryable=False)]},
    )

    with pytest.raises(LlmError, match="401"):
        await router.complete(ModelPurpose.CHAT, [ChatMessage(role="user", content="hello")])

    assert [created[0] for created in factory.created] == ["primary"]
    assert [attempt.status for attempt in sink.attempts] == ["permanent_error"]


@pytest.mark.asyncio
async def test_equal_priority_channels_use_deterministic_weighted_rotation() -> None:
    router, factory, _, _, _ = make_router(
        [channel("primary", weight=2), channel("secondary", weight=1)]
    )
    messages = [ChatMessage(role="user", content="hello")]

    for _ in range(3):
        await router.complete(ModelPurpose.CHAT, messages)

    assert [created[0] for created in factory.created] == [
        "primary",
        "primary",
        "secondary",
    ]


@pytest.mark.asyncio
async def test_configuration_is_cached_until_ttl_expires() -> None:
    now = 0.0

    def clock() -> float:
        return now

    router, _, _, source, _ = make_router([channel("primary")], clock=clock)
    messages = [ChatMessage(role="user", content="hello")]

    await router.complete(ModelPurpose.CHAT, messages)
    source.value = [channel("replacement")]
    await router.complete(ModelPurpose.CHAT, messages)
    now = 31.0
    await router.complete(ModelPurpose.CHAT, messages)

    assert source.calls == 2


@pytest.mark.asyncio
async def test_success_attempt_snapshots_price_and_computes_microdollar_cost() -> None:
    router, _, sink, _, _ = make_router([channel("primary")])

    await router.complete(ModelPurpose.CHAT, [ChatMessage(role="user", content="hello")])

    attempt = sink.attempts[0]
    assert attempt.input_price_per_million == Decimal("0.50")
    assert attempt.output_price_per_million == Decimal("1.50")
    assert attempt.cost_usd_micros == 80


@pytest.mark.asyncio
async def test_profile_context_selects_model_tier_and_attributes_attempt() -> None:
    router, factory, sink, _, _ = make_router(
        [channel("quality", tier="quality"), channel("economy", tier="economy")]
    )
    profile_id = uuid4()
    persona_version_id = uuid4()
    token = set_model_profile(
        tier="economy",
        profile_id=str(profile_id),
        persona_version_id=str(persona_version_id),
    )
    try:
        await router.for_purpose(ModelPurpose.CHAT).complete(
            [ChatMessage(role="user", content="hello")]
        )
    finally:
        reset_model_profile(token)

    assert [created[0] for created in factory.created] == ["economy"]
    assert sink.attempts[0].profile_id == str(profile_id)
    assert sink.attempts[0].persona_version_id == str(persona_version_id)
