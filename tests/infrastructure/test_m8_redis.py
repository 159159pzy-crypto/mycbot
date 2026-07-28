import asyncio
import hashlib
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from mybot.infrastructure.leases import LeaseManager, RedisLeaseBackend
from mybot.infrastructure.streams import RedisStreamBackend
from mybot.operator.auth import RedisAuthRateLimiter
from mybot.plugins.state import PluginRegistration, RedisPluginRegistrationStore

REDIS_URL = os.environ.get("MYBOT_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not REDIS_URL, reason="MYBOT_TEST_REDIS_URL is not configured"),
]


def redis_client() -> Redis:
    assert REDIS_URL is not None
    return Redis.from_url(REDIS_URL, decode_responses=True)  # type: ignore[no-any-return]


async def test_real_redis_lease_serializes_independent_workers() -> None:
    first_client = redis_client()
    second_client = redis_client()
    prefix = f"mybot:test:m8:lease:{uuid4().hex}"
    stable_key = "telegram:main:direct:42"
    key = f"{prefix}:{hashlib.sha256(stable_key.encode()).hexdigest()}"
    first = LeaseManager(
        RedisLeaseBackend(first_client),
        ttl_ms=90,
        retry_interval_seconds=0.005,
        prefix=prefix,
    )
    second = LeaseManager(
        RedisLeaseBackend(second_client),
        ttl_ms=90,
        retry_interval_seconds=0.005,
        prefix=prefix,
    )
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.12)
        active -= 1

    try:
        await asyncio.gather(first.run(stable_key, work), second.run(stable_key, work))
        assert peak == 1
        assert await first_client.exists(key) == 0
        assert stable_key not in key
    finally:
        await first_client.delete(key)
        await first_client.aclose()
        await second_client.aclose()


async def test_plugin_registration_survives_store_recreation() -> None:
    first_client = redis_client()
    second_client = redis_client()
    prefix = f"mybot:test:m8:plugins:{uuid4().hex}"
    key = f"{prefix}:runner-1"
    first = RedisPluginRegistrationStore(first_client, ttl_seconds=30, prefix=prefix)
    second = RedisPluginRegistrationStore(second_client, ttl_seconds=30, prefix=prefix)
    registration = PluginRegistration(
        runner_id="runner-1",
        protocol_version=2,
        manifests=[{"id": "example.persisted", "version": "1.0.0"}],
    )

    try:
        await first.save(registration)
        assert await second.load_all() == (registration,)
        await second.touch("runner-1")
        assert await first_client.ttl(key) > 0
    finally:
        await first_client.delete(key)
        await first_client.aclose()
        await second_client.aclose()


async def test_operator_auth_failures_are_shared_between_instances() -> None:
    first_client = redis_client()
    second_client = redis_client()
    prefix = f"mybot:test:m8:auth:{uuid4().hex}"
    client_identity = "203.0.113.7"
    key = f"{prefix}:{hashlib.sha256(client_identity.encode()).hexdigest()}"
    first = RedisAuthRateLimiter(
        first_client, max_failures=2, window_seconds=30, prefix=prefix
    )
    second = RedisAuthRateLimiter(
        second_client, max_failures=2, window_seconds=30, prefix=prefix
    )

    try:
        await first.record_failure(client_identity)
        assert await second.blocked(client_identity) is False
        await second.record_failure(client_identity)
        assert await first.blocked(client_identity) is True
        assert await first_client.ttl(key) > 0
    finally:
        await first_client.delete(key)
        await first_client.aclose()
        await second_client.aclose()


async def test_dead_letter_replay_is_atomic_across_api_instances() -> None:
    first_client = redis_client()
    second_client = redis_client()
    prefix = f"mybot:test:m8:replay:{uuid4().hex}"
    dead = f"{prefix}:dead"
    source = f"{prefix}:source"
    first = RedisStreamBackend(first_client)
    second = RedisStreamBackend(second_client)

    try:
        entry_id = await first.add(dead, "payload", maxlen=100)
        outcomes = await asyncio.gather(
            first.replay_dead_letter(dead, entry_id, source, "payload", maxlen=100),
            second.replay_dead_letter(dead, entry_id, source, "payload", maxlen=100),
            return_exceptions=True,
        )

        assert sum(isinstance(item, str) for item in outcomes) == 1
        assert sum(isinstance(item, KeyError) for item in outcomes) == 1
        assert await first.stream_len(source) == 1
        assert await first.stream_len(dead) == 0
    finally:
        await first_client.delete(dead, source)
        await first_client.aclose()
        await second_client.aclose()
