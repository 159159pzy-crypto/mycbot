import os

import pytest

from mybot.infrastructure.budget import TokenBudget
from mybot.infrastructure.streams import MemoryStreamBackend


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_budget(
    backend: MemoryStreamBackend,
    *,
    global_ceiling: int = 1_000,
    conversation_ceiling: int = 100,
) -> TokenBudget:
    return TokenBudget(
        backend=backend,
        global_daily_ceiling=global_ceiling,
        conversation_daily_ceiling=conversation_ceiling,
    )


@pytest.mark.asyncio
async def test_spend_accumulates_until_the_conversation_ceiling_blocks() -> None:
    budget = make_budget(MemoryStreamBackend())

    assert await budget.allows("conv-a", today="2026-07-26") is True
    await budget.consume("conv-a", 60, today="2026-07-26")
    assert await budget.allows("conv-a", today="2026-07-26") is True
    await budget.consume("conv-a", 60, today="2026-07-26")

    assert await budget.allows("conv-a", today="2026-07-26") is False
    assert await budget.allows("conv-b", today="2026-07-26") is True  # other conversations go on


@pytest.mark.asyncio
async def test_global_ceiling_blocks_every_conversation() -> None:
    budget = make_budget(MemoryStreamBackend(), global_ceiling=100)

    await budget.consume("conv-a", 100, today="2026-07-26")

    assert await budget.allows("conv-a", today="2026-07-26") is False
    assert await budget.allows("conv-b", today="2026-07-26") is False


@pytest.mark.asyncio
async def test_new_day_resets_the_counters() -> None:
    budget = make_budget(MemoryStreamBackend())

    await budget.consume("conv-a", 150, today="2026-07-26")

    assert await budget.allows("conv-a", today="2026-07-26") is False
    assert await budget.allows("conv-a", today="2026-07-27") is True


@pytest.mark.asyncio
async def test_zero_ceilings_never_block() -> None:
    budget = make_budget(MemoryStreamBackend(), global_ceiling=0, conversation_ceiling=0)

    await budget.consume("conv-a", 10_000_000, today="2026-07-26")

    assert await budget.allows("conv-a", today="2026-07-26") is True


@pytest.mark.asyncio
async def test_counters_expire_with_the_backend_clock() -> None:
    clock = Clock()
    backend = MemoryStreamBackend(clock=clock)
    budget = make_budget(backend)

    await budget.consume("conv-a", 150, today="2026-07-26")
    assert await budget.allows("conv-a", today="2026-07-26") is False

    clock.now += 200_000.0  # beyond the 48h TTL

    assert await budget.allows("conv-a", today="2026-07-26") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_redis_increment_round_trip() -> None:
    redis_url = os.environ.get("MYBOT_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("MYBOT_TEST_REDIS_URL is not configured")
    from mybot.infrastructure.streams import create_redis_backend

    backend = create_redis_backend(redis_url)
    key = "mybot:test:budget:counter"
    try:
        await backend.delete(key)
        first = await backend.increment(key, 40, ttl_seconds=60)
        second = await backend.increment(key, 2, ttl_seconds=60)
        assert (first, second) == (40, 42)
    finally:
        await backend.delete(key)
        await backend.aclose()
