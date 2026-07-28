import asyncio
import os

import pytest

from mybot.infrastructure.streams import (
    MemoryStreamBackend,
    StreamConsumer,
    StreamPublisher,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_consumer(
    backend: MemoryStreamBackend,
    *,
    consumer: str = "worker-1",
    max_attempts: int = 3,
) -> StreamConsumer:
    return StreamConsumer(
        backend,
        stream="mybot:ingest",
        group="agent-workers",
        consumer=consumer,
        dead_letter_stream="mybot:ingest:dead",
        max_attempts=max_attempts,
        dedupe_ttl_seconds=60,
        dedupe_prefix="mybot:seen",
        block_ms=10,
        claim_min_idle_ms=5_000,
    )


async def collect(backend: MemoryStreamBackend, stream: str) -> list[str]:
    return [payload for _, payload in await backend.entries(stream)]


@pytest.mark.asyncio
async def test_dead_letter_replay_clears_dedupe_and_removes_source_entry() -> None:
    backend = MemoryStreamBackend()
    dead_id = await backend.add("mybot:ingest:dead", "payload", maxlen=100)
    assert await backend.acquire_once("mybot:seen:ingest:event-1", ttl_seconds=60)

    replayed = await backend.replay_dead_letter(
        "mybot:ingest:dead",
        dead_id,
        "mybot:ingest",
        "payload",
        maxlen=100,
        dedupe_key="mybot:seen:ingest:event-1",
    )

    assert replayed
    assert not await backend.has_once("mybot:seen:ingest:event-1")
    assert await backend.entries("mybot:ingest:dead") == []
    assert [payload for _, payload in await backend.entries("mybot:ingest")] == ["payload"]

    with pytest.raises(KeyError):
        await backend.replay_dead_letter(
            "mybot:ingest:dead",
            dead_id,
            "mybot:ingest",
            "payload",
            maxlen=100,
            dedupe_key="mybot:seen:ingest:event-1",
        )


@pytest.mark.asyncio
async def test_publish_appends_and_trims_to_maxlen() -> None:
    backend = MemoryStreamBackend()
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=3)

    for index in range(5):
        await publisher.publish(f"payload-{index}")

    assert await collect(backend, "mybot:ingest") == ["payload-2", "payload-3", "payload-4"]


@pytest.mark.asyncio
async def test_successful_handling_acks_and_does_not_redeliver() -> None:
    backend = MemoryStreamBackend()
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    seen: list[str] = []

    await publisher.publish("one")
    await publisher.publish("two")
    handled = await consumer.process_available(seen.append)
    handled += await consumer.process_available(seen.append)

    assert handled == 2
    assert seen == ["one", "two"]
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_crashing_handler_leaves_entry_pending_for_reclaim() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    crashing = make_consumer(backend, consumer="worker-1")
    recovering = make_consumer(backend, consumer="worker-2")
    seen: list[str] = []

    def explode(payload: str) -> None:
        raise RuntimeError("boom")

    await publisher.publish("fragile")
    await crashing.process_available(explode)
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 1

    clock.now += 10.0
    await recovering.process_available(seen.append)

    assert seen == ["fragile"]
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_failed_delivery_does_not_mark_dedupe_key_before_reclaim() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    calls = 0

    def fail_once(payload: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")

    await publisher.publish("same-envelope")
    await consumer.process_available(fail_once, dedupe_key=lambda payload: payload)
    clock.now += 10.0
    await consumer.process_available(fail_once, dedupe_key=lambda payload: payload)

    assert calls == 2
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_entry_exceeding_max_attempts_moves_to_dead_letter_stream() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend, max_attempts=2)

    def explode(payload: str) -> None:
        raise RuntimeError("boom")

    await publisher.publish("poison")
    for _ in range(3):
        await consumer.process_available(explode)
        clock.now += 10.0

    dead = await collect(backend, "mybot:ingest:dead")
    assert dead == ["poison"]
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_duplicate_dedupe_keys_are_acked_and_skipped() -> None:
    backend = MemoryStreamBackend()
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    seen: list[str] = []

    await publisher.publish("payload-a")
    await publisher.publish("payload-a")
    await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)
    await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)

    assert seen == ["payload-a"]
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_dedupe_keys_expire_after_ttl() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    seen: list[str] = []

    await publisher.publish("payload-a")
    await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)
    clock.now += 120.0
    await publisher.publish("payload-a")
    await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)

    assert seen == ["payload-a", "payload-a"]


@pytest.mark.asyncio
async def test_malformed_payload_dead_letters_immediately() -> None:
    backend = MemoryStreamBackend()
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    seen: list[str] = []

    def reject(payload: str) -> str:
        raise ValueError("not decodable")

    await publisher.publish("garbage")
    await consumer.process_available(seen.append, dedupe_key=reject)

    assert seen == []
    assert await collect(backend, "mybot:ingest:dead") == ["garbage"]
    assert await backend.pending_count("mybot:ingest", "agent-workers") == 0


@pytest.mark.asyncio
async def test_run_loop_processes_until_stop_and_cancellation_propagates() -> None:
    backend = MemoryStreamBackend()
    publisher = StreamPublisher(backend=backend, stream="mybot:ingest", maxlen=100)
    consumer = make_consumer(backend)
    stop_event = asyncio.Event()
    seen: list[str] = []

    async def handler(payload: str) -> None:
        seen.append(payload)

    task = asyncio.create_task(consumer.run(handler, stop_event=stop_event))
    await publisher.publish("live-one")
    for _ in range(200):
        if seen == ["live-one"]:
            break
        await asyncio.sleep(0.01)
    assert seen == ["live-one"]
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    running = asyncio.create_task(consumer.run(handler, stop_event=asyncio.Event()))
    await asyncio.sleep(0.05)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_redis_round_trip_with_ack_and_dedupe() -> None:
    redis_url = os.environ.get("MYBOT_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("MYBOT_TEST_REDIS_URL is not configured")
    from mybot.infrastructure.streams import RedisStreamBackend, create_redis_backend

    backend: RedisStreamBackend = create_redis_backend(redis_url)
    stream = "mybot:test:ingest"
    dead = "mybot:test:ingest:dead"
    await backend.delete(stream, dead)
    publisher = StreamPublisher(backend=backend, stream=stream, maxlen=100)
    consumer = StreamConsumer(
        backend,
        stream=stream,
        group="test-workers",
        consumer="worker-int",
        dead_letter_stream=dead,
        max_attempts=2,
        dedupe_ttl_seconds=30,
        dedupe_prefix="mybot:test:seen",
        block_ms=50,
        claim_min_idle_ms=100,
    )
    seen: list[str] = []
    try:
        await publisher.publish("real-one")
        await publisher.publish("real-one")
        await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)
        await consumer.process_available(seen.append, dedupe_key=lambda payload: payload)

        assert seen == ["real-one"]
        assert await backend.pending_count(stream, "test-workers") == 0
    finally:
        await backend.delete(stream, dead)
        await backend.aclose()
