"""Purpose-aware model channel routing with cooldowns and attempt accounting."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol

import httpx
from pydantic import Field, JsonValue, field_validator
from redis.asyncio import Redis

from mybot.contracts.common import FrozenModel, NonEmptyStr
from mybot.infrastructure.embeddings import EmbeddingClient, EmbeddingError
from mybot.infrastructure.llm import ChatMessage, LlmClient, LlmError, LlmReply
from mybot.infrastructure.telemetry import start_span

if TYPE_CHECKING:
    from mybot.settings import Settings

MODEL_CHANNELS_KEY = "models.channels"
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_conversation_context: ContextVar[str | None] = ContextVar(
    "mybot_model_conversation_id", default=None
)
_tier_context: ContextVar[str] = ContextVar("mybot_model_tier", default="default")
_profile_context: ContextVar[str | None] = ContextVar("mybot_model_profile_id", default=None)
_persona_version_context: ContextVar[str | None] = ContextVar(
    "mybot_model_persona_version_id", default=None
)


class ModelPurpose(StrEnum):
    CHAT = "chat"
    MEMORY = "memory"
    EMBEDDING = "embedding"
    VISION = "vision"


class ModelTarget(FrozenModel):
    model: NonEmptyStr
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)


class ModelChannel(FrozenModel):
    name: NonEmptyStr = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    base_url: NonEmptyStr
    api_key_env: NonEmptyStr | None = None
    priority: int = Field(default=0, ge=0, le=10_000)
    weight: int = Field(default=1, ge=1, le=1_000)
    tier: NonEmptyStr = "default"
    enabled: bool = True
    model_map: Mapping[ModelPurpose, ModelTarget]

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        if value is not None and _ENV_NAME.fullmatch(value) is None:
            raise ValueError("api_key_env must be an environment variable name")
        return value

    @field_validator("model_map")
    @classmethod
    def validate_model_map(
        cls, value: Mapping[ModelPurpose, ModelTarget]
    ) -> Mapping[ModelPurpose, ModelTarget]:
        if not value:
            raise ValueError("model_map must declare at least one purpose")
        return value


type AttemptStatus = Literal["success", "retryable_error", "permanent_error"]


@dataclass(slots=True, frozen=True)
class ModelCallAttempt:
    purpose: ModelPurpose
    channel: str
    model: str
    status: AttemptStatus
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    conversation_id: str | None = None
    profile_id: str | None = None
    persona_version_id: str | None = None
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    cost_usd_micros: int | None = None
    error_code: str | None = None


@dataclass(slots=True, frozen=True)
class EmbeddingBatch:
    """Embedding vectors together with the model that actually produced them."""

    vectors: list[list[float]]
    model: str


class ModelConfigSource(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


class ModelAttemptSink(Protocol):
    async def record(self, attempt: ModelCallAttempt) -> None: ...


class ModelCooldowns(Protocol):
    async def is_cooling(self, channel: str, purpose: ModelPurpose) -> bool: ...

    async def cool(self, channel: str, purpose: ModelPurpose, *, ttl_seconds: int) -> None: ...


class ChannelClient(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


type ChannelClientFactory = Callable[[ModelChannel, ModelPurpose, str | None], ChannelClient]


@dataclass(slots=True)
class MemoryModelCooldowns:
    clock: Callable[[], float] = monotonic
    _expiry: dict[tuple[str, ModelPurpose], float] = field(
        default_factory=dict[tuple[str, ModelPurpose], float]
    )

    async def is_cooling(self, channel: str, purpose: ModelPurpose) -> bool:
        return self._expiry.get((channel, purpose), 0.0) > self.clock()

    async def cool(self, channel: str, purpose: ModelPurpose, *, ttl_seconds: int) -> None:
        self._expiry[(channel, purpose)] = self.clock() + ttl_seconds


@dataclass(slots=True)
class RedisModelCooldowns:
    client: Redis
    prefix: str = "mybot:model:cooldown"

    async def is_cooling(self, channel: str, purpose: ModelPurpose) -> bool:
        return bool(await self.client.exists(self._key(channel, purpose)))

    async def cool(self, channel: str, purpose: ModelPurpose, *, ttl_seconds: int) -> None:
        await self.client.set(self._key(channel, purpose), "1", ex=ttl_seconds)

    def _key(self, channel: str, purpose: ModelPurpose) -> str:
        return f"{self.prefix}:{purpose.value}:{channel}"


class ModelRoutingError(RuntimeError):
    """Runtime channel configuration is absent or invalid for a purpose."""


@dataclass(slots=True)
class ModelRouter:
    config: ModelConfigSource
    fallback_channels: tuple[ModelChannel, ...]
    cooldowns: ModelCooldowns
    attempts: ModelAttemptSink
    client_factory: ChannelClientFactory
    secret_lookup: Callable[[str], str | None]
    cache_ttl_seconds: float = 30.0
    cooldown_seconds: int = 60
    clock: Callable[[], float] = monotonic
    _cached_channels: tuple[ModelChannel, ...] | None = None
    _cache_expires_at: float = 0.0
    _cursors: dict[ModelPurpose, int] = field(
        default_factory=dict[ModelPurpose, int]
    )

    async def complete(
        self,
        purpose: ModelPurpose,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
        conversation_id: str | None = None,
        tier: str = "default",
        profile_id: str | None = None,
        persona_version_id: str | None = None,
    ) -> LlmReply:
        candidates = await self._candidates(purpose, tier=tier)
        last_error: LlmError | None = None
        for channel in candidates:
            target = channel.model_map[purpose]
            started = self.clock()
            try:
                client = self._client(channel, purpose)
                with start_span(
                    "llm.call",
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.provider.name": channel.name,
                        "gen_ai.request.model": target.model,
                        "mybot.model_purpose": purpose.value,
                    },
                ):
                    reply = await client.complete(messages, tools=tools)
            except LlmError as error:
                last_error = error
                await self._record_error(
                    channel,
                    purpose,
                    target,
                    error,
                    started,
                    conversation_id,
                    profile_id,
                    persona_version_id,
                )
                if not error.retryable:
                    raise
                await self.cooldowns.cool(channel.name, purpose, ttl_seconds=self.cooldown_seconds)
                continue
            await self._record_success(
                channel,
                purpose,
                target,
                reply,
                started,
                conversation_id,
                profile_id,
                persona_version_id,
            )
            return reply
        if last_error is not None:
            raise last_error
        raise LlmError(f"no available model channel for {purpose.value}", retryable=True)

    async def embed_with_model(
        self,
        texts: Sequence[str],
        *,
        conversation_id: str | None = None,
        tier: str = "default",
        profile_id: str | None = None,
        persona_version_id: str | None = None,
    ) -> EmbeddingBatch:
        purpose = ModelPurpose.EMBEDDING
        candidates = await self._candidates(purpose, tier=tier)
        last_error: EmbeddingError | None = None
        for channel in candidates:
            target = channel.model_map[purpose]
            started = self.clock()
            try:
                with start_span(
                    "llm.embedding",
                    attributes={
                        "gen_ai.operation.name": "embeddings",
                        "gen_ai.provider.name": channel.name,
                        "gen_ai.request.model": target.model,
                        "mybot.embedding_inputs": len(texts),
                    },
                ):
                    vectors = await self._client(channel, purpose).embed(texts)
            except EmbeddingError as error:
                last_error = error
                await self._record_embedding_error(
                    channel,
                    target,
                    error,
                    started,
                    conversation_id,
                    profile_id,
                    persona_version_id,
                )
                if not error.retryable:
                    raise
                await self.cooldowns.cool(channel.name, purpose, ttl_seconds=self.cooldown_seconds)
                continue
            input_tokens = sum(max(1, len(text) // 4) for text in texts)
            await self.attempts.record(
                ModelCallAttempt(
                    purpose=purpose,
                    channel=channel.name,
                    model=target.model,
                    status="success",
                    prompt_tokens=input_tokens,
                    latency_ms=_latency_ms(started, self.clock()),
                    conversation_id=conversation_id,
                    profile_id=profile_id,
                    persona_version_id=persona_version_id,
                    input_price_per_million=target.input_price_per_million,
                    output_price_per_million=target.output_price_per_million,
                    cost_usd_micros=calculate_cost_micros(target, input_tokens, 0),
                )
            )
            return EmbeddingBatch(vectors=vectors, model=target.model)
        if last_error is not None:
            raise last_error
        raise EmbeddingError("no available embedding channel", retryable=True)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        conversation_id: str | None = None,
        tier: str = "default",
        profile_id: str | None = None,
        persona_version_id: str | None = None,
    ) -> list[list[float]]:
        batch = await self.embed_with_model(
            texts,
            conversation_id=conversation_id,
            tier=tier,
            profile_id=profile_id,
            persona_version_id=persona_version_id,
        )
        return batch.vectors

    def for_purpose(self, purpose: ModelPurpose) -> RoutedLlmClient:
        return RoutedLlmClient(router=self, purpose=purpose)

    def embeddings(self) -> RoutedEmbeddingClient:
        return RoutedEmbeddingClient(router=self)

    def invalidate(self) -> None:
        self._cached_channels = None
        self._cache_expires_at = 0.0

    async def _channels(self) -> tuple[ModelChannel, ...]:
        now = self.clock()
        if self._cached_channels is not None and now < self._cache_expires_at:
            return self._cached_channels
        raw = await self.config.get(MODEL_CHANNELS_KEY)
        channels = self.fallback_channels
        if raw is not None:
            if not isinstance(raw, list):
                raise ModelRoutingError("models.channels must be a JSON list")
            channels = tuple(ModelChannel.model_validate(item) for item in raw)
        self._cached_channels = channels
        self._cache_expires_at = now + self.cache_ttl_seconds
        return channels

    async def _candidates(
        self, purpose: ModelPurpose, *, tier: str = "default"
    ) -> tuple[ModelChannel, ...]:
        channels = [
            channel
            for channel in await self._channels()
            if channel.enabled and purpose in channel.model_map
        ]
        matching = [channel for channel in channels if channel.tier == tier]
        if matching:
            channels = matching
        elif tier != "default":
            defaults = [channel for channel in channels if channel.tier == "default"]
            if defaults:
                channels = defaults
        ordered: list[ModelChannel] = []
        for priority in sorted({channel.priority for channel in channels}):
            group = sorted(
                (channel for channel in channels if channel.priority == priority),
                key=lambda channel: channel.name,
            )
            pool = [channel for channel in group for _ in range(channel.weight)]
            if not pool:
                continue
            cursor = self._cursors.get(purpose, 0) % len(pool)
            self._cursors[purpose] = self._cursors.get(purpose, 0) + 1
            for offset in range(len(pool)):
                channel = pool[(cursor + offset) % len(pool)]
                if channel not in ordered:
                    ordered.append(channel)
        available: list[ModelChannel] = []
        for channel in ordered:
            if not await self.cooldowns.is_cooling(channel.name, purpose):
                available.append(channel)
        return tuple(available)

    def _client(self, channel: ModelChannel, purpose: ModelPurpose) -> ChannelClient:
        api_key = (
            self.secret_lookup(channel.api_key_env) if channel.api_key_env is not None else None
        )
        return self.client_factory(channel, purpose, api_key)

    async def _record_success(
        self,
        channel: ModelChannel,
        purpose: ModelPurpose,
        target: ModelTarget,
        reply: LlmReply,
        started: float,
        conversation_id: str | None,
        profile_id: str | None,
        persona_version_id: str | None,
    ) -> None:
        await self.attempts.record(
            ModelCallAttempt(
                purpose=purpose,
                channel=channel.name,
                model=reply.model,
                status="success",
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                latency_ms=_latency_ms(started, self.clock()),
                conversation_id=conversation_id,
                profile_id=profile_id,
                persona_version_id=persona_version_id,
                input_price_per_million=target.input_price_per_million,
                output_price_per_million=target.output_price_per_million,
                cost_usd_micros=calculate_cost_micros(
                    target, reply.prompt_tokens, reply.completion_tokens
                ),
            )
        )

    async def _record_error(
        self,
        channel: ModelChannel,
        purpose: ModelPurpose,
        target: ModelTarget,
        error: LlmError,
        started: float,
        conversation_id: str | None,
        profile_id: str | None,
        persona_version_id: str | None,
    ) -> None:
        await self.attempts.record(
            ModelCallAttempt(
                purpose=purpose,
                channel=channel.name,
                model=target.model,
                status="retryable_error" if error.retryable else "permanent_error",
                latency_ms=_latency_ms(started, self.clock()),
                conversation_id=conversation_id,
                profile_id=profile_id,
                persona_version_id=persona_version_id,
                input_price_per_million=target.input_price_per_million,
                output_price_per_million=target.output_price_per_million,
                error_code=type(error).__name__,
            )
        )

    async def _record_embedding_error(
        self,
        channel: ModelChannel,
        target: ModelTarget,
        error: EmbeddingError,
        started: float,
        conversation_id: str | None,
        profile_id: str | None,
        persona_version_id: str | None,
    ) -> None:
        await self.attempts.record(
            ModelCallAttempt(
                purpose=ModelPurpose.EMBEDDING,
                channel=channel.name,
                model=target.model,
                status="retryable_error" if error.retryable else "permanent_error",
                latency_ms=_latency_ms(started, self.clock()),
                conversation_id=conversation_id,
                profile_id=profile_id,
                persona_version_id=persona_version_id,
                input_price_per_million=target.input_price_per_million,
                output_price_per_million=target.output_price_per_million,
                error_code=type(error).__name__,
            )
        )


@dataclass(slots=True)
class RoutedLlmClient:
    router: ModelRouter
    purpose: ModelPurpose

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply:
        return await self.router.complete(
            self.purpose,
            messages,
            tools=tools,
            conversation_id=_conversation_context.get(),
            tier=_tier_context.get(),
            profile_id=_profile_context.get(),
            persona_version_id=_persona_version_context.get(),
        )


@dataclass(slots=True)
class RoutedEmbeddingClient:
    router: ModelRouter

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = await self.embed_with_model(texts)
        return batch.vectors

    async def embed_with_model(self, texts: Sequence[str]) -> EmbeddingBatch:
        return await self.router.embed_with_model(
            texts,
            conversation_id=_conversation_context.get(),
            tier=_tier_context.get(),
            profile_id=_profile_context.get(),
            persona_version_id=_persona_version_context.get(),
        )


@dataclass(slots=True)
class OpenAIChannelClient:
    llm: LlmClient
    embedding: EmbeddingClient

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]] | None = None,
    ) -> LlmReply:
        return await self.llm.complete(messages, tools=tools)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.embedding.embed(texts)


def openai_client_factory(
    client: httpx.AsyncClient,
    *,
    temperature: float,
    max_output_tokens: int,
    timeout_seconds: float,
) -> ChannelClientFactory:
    def create(
        channel: ModelChannel, purpose: ModelPurpose, api_key: str | None
    ) -> OpenAIChannelClient:
        target = channel.model_map[purpose]
        return OpenAIChannelClient(
            llm=LlmClient(
                client=client,
                base_url=channel.base_url,
                api_key=api_key,
                model=target.model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
                max_retries=0,
            ),
            embedding=EmbeddingClient(
                client=client,
                base_url=channel.base_url,
                api_key=api_key,
                model=target.model,
                timeout_seconds=min(timeout_seconds, 60.0),
                max_retries=0,
            ),
        )

    return create


def set_model_conversation(conversation_id: str) -> Token[str | None]:
    return _conversation_context.set(conversation_id)


def reset_model_conversation(token: Token[str | None]) -> None:
    _conversation_context.reset(token)


type ModelProfileToken = tuple[Token[str], Token[str | None], Token[str | None]]


def set_model_profile(
    *, tier: str, profile_id: str | None, persona_version_id: str | None
) -> ModelProfileToken:
    return (
        _tier_context.set(tier),
        _profile_context.set(profile_id),
        _persona_version_context.set(persona_version_id),
    )


def reset_model_profile(token: ModelProfileToken) -> None:
    tier_token, profile_token, persona_token = token
    _persona_version_context.reset(persona_token)
    _profile_context.reset(profile_token)
    _tier_context.reset(tier_token)


def legacy_model_channels(settings: Settings) -> tuple[ModelChannel, ...]:
    """Translate v1 environment settings into non-persistent fallback channels."""

    channels: list[ModelChannel] = []
    if settings.llm_base_url is not None:
        channels.append(
            ModelChannel(
                name="legacy-chat",
                base_url=settings.llm_base_url,
                api_key_env="MYBOT_LLM_API_KEY",
                model_map={
                    ModelPurpose.CHAT: ModelTarget(model=settings.llm_model),
                    ModelPurpose.MEMORY: ModelTarget(model=settings.llm_model),
                    ModelPurpose.VISION: ModelTarget(model=settings.llm_model),
                },
            )
        )
    embedding_base = settings.embedding_base_url or settings.llm_base_url
    if embedding_base is not None:
        channels.append(
            ModelChannel(
                name="legacy-embedding",
                base_url=embedding_base,
                api_key_env=(
                    "MYBOT_EMBEDDING_API_KEY"
                    if settings.embedding_api_key is not None
                    else "MYBOT_LLM_API_KEY"
                ),
                model_map={ModelPurpose.EMBEDDING: ModelTarget(model=settings.embedding_model)},
            )
        )
    return tuple(channels)


def _latency_ms(started: float, finished: float) -> int:
    return max(0, int((finished - started) * 1_000))


def calculate_cost_micros(
    target: ModelTarget, prompt_tokens: int, completion_tokens: int
) -> int | None:
    if target.input_price_per_million is None and target.output_price_per_million is None:
        return None
    cost = Decimal(prompt_tokens) * (target.input_price_per_million or Decimal(0)) + Decimal(
        completion_tokens
    ) * (target.output_price_per_million or Decimal(0))
    return int(cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
