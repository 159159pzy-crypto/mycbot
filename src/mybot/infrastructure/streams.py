"""At-least-once stream transport between the gateway and worker roles."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol, cast

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger("mybot.streams")

type StreamEntry = tuple[str, str]
type Handler = Callable[[str], Awaitable[None] | None]
type DedupeKey = Callable[[str], str | None]
_delivery_attempt: ContextVar[int] = ContextVar("mybot_stream_delivery_attempt", default=1)


def current_delivery_attempt() -> int:
    """Return the delivery count for the stream handler currently running."""

    return _delivery_attempt.get()


class StreamBackend(Protocol):
    """The narrow stream surface the broker requires from a transport."""

    async def add(self, stream: str, payload: str, *, maxlen: int) -> str: ...

    async def ensure_group(self, stream: str, group: str) -> None: ...

    async def read_new(
        self, stream: str, group: str, consumer: str, *, count: int, block_ms: int
    ) -> list[StreamEntry]: ...

    async def claim_stale(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int
    ) -> list[StreamEntry]: ...

    async def delivery_count(self, stream: str, group: str, entry_id: str) -> int: ...

    async def ack(self, stream: str, group: str, entry_id: str) -> None: ...

    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool: ...

    async def has_once(self, key: str) -> bool: ...


@dataclass(slots=True)
class StreamPublisher:
    backend: StreamBackend
    stream: str
    maxlen: int

    async def publish(self, payload: str) -> str:
        return await self.backend.add(self.stream, payload, maxlen=self.maxlen)


class StreamConsumer:
    """Consumer-group reader with explicit acks, dedupe, and dead-lettering."""

    def __init__(
        self,
        backend: StreamBackend,
        *,
        stream: str,
        group: str,
        consumer: str,
        dead_letter_stream: str,
        max_attempts: int,
        dedupe_ttl_seconds: int,
        dedupe_prefix: str,
        block_ms: int = 1_000,
        claim_min_idle_ms: int = 30_000,
        read_count: int = 16,
        dead_letter_maxlen: int = 1_000,
    ) -> None:
        self._backend = backend
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._dead_letter_stream = dead_letter_stream
        self._max_attempts = max_attempts
        self._dedupe_ttl_seconds = dedupe_ttl_seconds
        self._dedupe_prefix = dedupe_prefix
        self._block_ms = block_ms
        self._claim_min_idle_ms = claim_min_idle_ms
        self._read_count = read_count
        self._dead_letter_maxlen = dead_letter_maxlen
        self._group_ready = False

    async def process_available(
        self,
        handler: Handler,
        *,
        dedupe_key: DedupeKey | None = None,
        block_ms: int | None = None,
    ) -> int:
        """Process one batch of claimed-stale plus new entries; return handled count."""

        await self._ensure_group()
        handled = 0
        stale = await self._backend.claim_stale(
            self._stream,
            self._group,
            self._consumer,
            min_idle_ms=self._claim_min_idle_ms,
            count=self._read_count,
        )
        for entry_id, payload in stale:
            attempts = await self._backend.delivery_count(self._stream, self._group, entry_id)
            if attempts > self._max_attempts:
                await self._dead_letter(entry_id, payload, reason="max_attempts_exceeded")
                continue
            handled += await self._handle(
                entry_id, payload, handler, dedupe_key, attempts=attempts
            )
        fresh = await self._backend.read_new(
            self._stream,
            self._group,
            self._consumer,
            count=self._read_count,
            block_ms=self._block_ms if block_ms is None else block_ms,
        )
        for entry_id, payload in fresh:
            handled += await self._handle(entry_id, payload, handler, dedupe_key, attempts=1)
        return handled

    async def run(
        self,
        handler: Handler,
        *,
        stop_event: asyncio.Event,
        dedupe_key: DedupeKey | None = None,
    ) -> None:
        """Consume until the stop event is set; cancellation always propagates."""

        while not stop_event.is_set():
            try:
                await self.process_available(handler, dedupe_key=dedupe_key)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stream_consumer_iteration_failed", stream=self._stream)
                await asyncio.sleep(self._block_ms / 1000)
            await asyncio.sleep(0)

    async def _ensure_group(self) -> None:
        if not self._group_ready:
            await self._backend.ensure_group(self._stream, self._group)
            self._group_ready = True

    async def _handle(
        self,
        entry_id: str,
        payload: str,
        handler: Handler,
        dedupe_key: DedupeKey | None,
        *,
        attempts: int,
    ) -> int:
        completed_key: str | None = None
        if dedupe_key is not None:
            try:
                key = dedupe_key(payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._dead_letter(entry_id, payload, reason=type(error).__name__)
                return 0
            if key is not None:
                completed_key = f"{self._dedupe_prefix}:{key}"
                if await self._backend.has_once(completed_key):
                    await self._backend.ack(self._stream, self._group, entry_id)
                    return 0
        token: Token[int] = _delivery_attempt.set(attempts)
        try:
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "stream_entry_handler_failed", stream=self._stream, entry_id=entry_id
            )
            return 0
        finally:
            _delivery_attempt.reset(token)
        if completed_key is not None:
            await self._backend.acquire_once(
                completed_key, ttl_seconds=self._dedupe_ttl_seconds
            )
        await self._backend.ack(self._stream, self._group, entry_id)
        return 1

    async def _dead_letter(self, entry_id: str, payload: str, *, reason: str) -> None:
        logger.warning(
            "stream_entry_dead_lettered",
            stream=self._stream,
            entry_id=entry_id,
            reason=reason,
        )
        await self._backend.add(
            self._dead_letter_stream, payload, maxlen=self._dead_letter_maxlen
        )
        await self._backend.ack(self._stream, self._group, entry_id)


@dataclass(slots=True)
class _PendingEntry:
    consumer: str
    delivery_count: int
    since: float


@dataclass(slots=True)
class _GroupState:
    next_index: int = 0
    pending: dict[str, _PendingEntry] = field(default_factory=dict[str, _PendingEntry])


class MemoryStreamBackend:
    """In-memory backend with consumer-group semantics for tests and local runs."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._streams: dict[str, list[tuple[int, str]]] = {}
        self._sequence: dict[str, int] = {}
        self._groups: dict[tuple[str, str], _GroupState] = {}
        self._once: dict[str, float] = {}
        self._counters: dict[str, tuple[int, float]] = {}

    async def add(self, stream: str, payload: str, *, maxlen: int) -> str:
        sequence = self._sequence.get(stream, 0) + 1
        self._sequence[stream] = sequence
        entries = self._streams.setdefault(stream, [])
        entries.append((sequence, payload))
        while len(entries) > maxlen:
            entries.pop(0)
        return f"{sequence}-0"

    async def ensure_group(self, stream: str, group: str) -> None:
        self._groups.setdefault((stream, group), _GroupState())
        self._streams.setdefault(stream, [])

    async def read_new(
        self, stream: str, group: str, consumer: str, *, count: int, block_ms: int
    ) -> list[StreamEntry]:
        state = self._groups.setdefault((stream, group), _GroupState())
        delivered: list[StreamEntry] = []
        for sequence, payload in self._streams.get(stream, []):
            if sequence <= state.next_index or len(delivered) >= count:
                continue
            entry_id = f"{sequence}-0"
            state.pending[entry_id] = _PendingEntry(
                consumer=consumer, delivery_count=1, since=self._clock()
            )
            state.next_index = sequence
            delivered.append((entry_id, payload))
        if not delivered:
            # Emulate XREADGROUP BLOCK so callers always yield to the event loop.
            await asyncio.sleep(block_ms / 1000)
        return delivered

    async def claim_stale(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int
    ) -> list[StreamEntry]:
        state = self._groups.setdefault((stream, group), _GroupState())
        now = self._clock()
        claimed: list[StreamEntry] = []
        payloads = {f"{sequence}-0": payload for sequence, payload in self._streams.get(stream, [])}
        for entry_id, pending in sorted(state.pending.items()):
            if len(claimed) >= count:
                break
            if (now - pending.since) * 1000 < min_idle_ms:
                continue
            payload = payloads.get(entry_id)
            if payload is None:
                del state.pending[entry_id]
                continue
            pending.consumer = consumer
            pending.delivery_count += 1
            pending.since = now
            claimed.append((entry_id, payload))
        return claimed

    async def delivery_count(self, stream: str, group: str, entry_id: str) -> int:
        state = self._groups.setdefault((stream, group), _GroupState())
        pending = state.pending.get(entry_id)
        return 0 if pending is None else pending.delivery_count

    async def ack(self, stream: str, group: str, entry_id: str) -> None:
        state = self._groups.setdefault((stream, group), _GroupState())
        state.pending.pop(entry_id, None)

    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool:
        now = self._clock()
        expiry = self._once.get(key)
        if expiry is not None and expiry > now:
            return False
        self._once[key] = now + ttl_seconds
        return True

    async def has_once(self, key: str) -> bool:
        expiry = self._once.get(key)
        return expiry is not None and expiry > self._clock()

    async def increment(self, key: str, amount: int, *, ttl_seconds: int) -> int:
        now = self._clock()
        value, expiry = self._counters.get(key, (0, 0.0))
        if expiry <= now:
            value = 0
            expiry = now + ttl_seconds
        value += amount
        self._counters[key] = (value, expiry)
        return value

    async def entries(self, stream: str) -> list[StreamEntry]:
        return [(f"{sequence}-0", payload) for sequence, payload in self._streams.get(stream, [])]

    async def stream_len(self, stream: str) -> int:
        return len(self._streams.get(stream, []))

    async def pending_count(self, stream: str, group: str) -> int:
        state = self._groups.setdefault((stream, group), _GroupState())
        return len(state.pending)

    async def aclose(self) -> None:
        return None


class RedisStreamBackend:
    """Redis Streams implementation of the broker backend."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def add(self, stream: str, payload: str, *, maxlen: int) -> str:
        entry_id = await self._client.xadd(  # pyright: ignore[reportUnknownMemberType]
            stream, {"payload": payload}, maxlen=maxlen, approximate=True
        )
        return _as_text(entry_id)

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(  # pyright: ignore[reportUnknownMemberType]
                stream, group, id="0", mkstream=True
            )
        except Exception as error:  # redis raises ResponseError BUSYGROUP when it exists
            if "BUSYGROUP" not in str(error):
                raise

    async def read_new(
        self, stream: str, group: str, consumer: str, *, count: int, block_ms: int
    ) -> list[StreamEntry]:
        response = cast(
            list[tuple[object, list[tuple[object, dict[object, object]]]]],
            await self._client.xreadgroup(  # pyright: ignore[reportUnknownMemberType]
                group, consumer, {stream: ">"}, count=count, block=block_ms
            ),
        )
        entries: list[StreamEntry] = []
        for _, stream_entries in response or []:
            for entry_id, fields in stream_entries:
                payload = _payload_from_fields(fields)
                if payload is not None:
                    entries.append((_as_text(entry_id), payload))
        return entries

    async def claim_stale(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int
    ) -> list[StreamEntry]:
        await self.ensure_group(stream, group)
        response = cast(
            tuple[object, list[tuple[object, dict[object, object]]], list[object]],
            await self._client.xautoclaim(  # pyright: ignore[reportUnknownMemberType]
                stream,
                group,
                consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            ),
        )
        entries: list[StreamEntry] = []
        for entry_id, fields in response[1]:
            payload = _payload_from_fields(fields)
            if payload is not None:
                entries.append((_as_text(entry_id), payload))
        return entries

    async def delivery_count(self, stream: str, group: str, entry_id: str) -> int:
        response = cast(
            list[dict[str, object]],
            await self._client.xpending_range(  # pyright: ignore[reportUnknownMemberType]
                stream, group, min=entry_id, max=entry_id, count=1
            ),
        )
        if not response:
            return 0
        times_delivered = response[0].get("times_delivered")
        if isinstance(times_delivered, int):
            return times_delivered
        return 0

    async def ack(self, stream: str, group: str, entry_id: str) -> None:
        await self._client.xack(  # pyright: ignore[reportUnknownMemberType]
            stream, group, entry_id
        )

    async def acquire_once(self, key: str, *, ttl_seconds: int) -> bool:
        acquired = await self._client.set(key, "1", nx=True, ex=ttl_seconds)
        return bool(acquired)

    async def has_once(self, key: str) -> bool:
        return bool(await self._client.exists(key))

    async def increment(self, key: str, amount: int, *, ttl_seconds: int) -> int:
        value = await self._client.incrby(  # pyright: ignore[reportUnknownMemberType]
            key, amount
        )
        if value == amount:
            await self._client.expire(  # pyright: ignore[reportUnknownMemberType]
                key, ttl_seconds
            )
        return int(value)

    async def delete(self, *streams: str) -> None:
        await self._client.delete(*streams)  # pyright: ignore[reportUnknownMemberType]

    async def stream_len(self, stream: str) -> int:
        try:
            length = await self._client.xlen(stream)  # pyright: ignore[reportUnknownMemberType]
        except Exception:
            return 0
        return int(length) if isinstance(length, int) else 0

    async def pending_count(self, stream: str, group: str) -> int:
        response = cast(
            dict[str, object],
            await self._client.xpending(  # pyright: ignore[reportUnknownMemberType]
                stream, group
            ),
        )
        pending = response.get("pending")
        return pending if isinstance(pending, int) else 0

    async def aclose(self) -> None:
        await self._client.aclose()


def create_redis_backend(redis_url: str) -> RedisStreamBackend:
    client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        redis_url, decode_responses=True
    )
    return RedisStreamBackend(client)


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _payload_from_fields(fields: dict[object, object]) -> str | None:
    for key, value in fields.items():
        if _as_text(key) == "payload":
            return _as_text(value)
    return None
