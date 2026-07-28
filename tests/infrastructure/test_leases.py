import asyncio
from dataclasses import dataclass

import pytest

from mybot.infrastructure.leases import LeaseLostError, LeaseManager, MemoryLeaseBackend


@dataclass
class LostLeaseBackend:
    released: bool = False

    async def acquire(self, key: str, token: str, *, ttl_ms: int) -> bool:
        return True

    async def renew(self, key: str, token: str, *, ttl_ms: int) -> bool:
        return False

    async def release(self, key: str, token: str) -> bool:
        self.released = True
        return True


@pytest.mark.asyncio
async def test_shared_lease_serializes_same_conversation() -> None:
    backend = MemoryLeaseBackend()
    first = LeaseManager(backend, ttl_ms=200, retry_interval_seconds=0.005)
    second = LeaseManager(backend, ttl_ms=200, retry_interval_seconds=0.005)
    active = 0
    peak = 0
    order: list[str] = []

    async def work(name: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(f"{name}:start")
        await asyncio.sleep(0.03)
        order.append(f"{name}:end")
        active -= 1
        return name

    results = await asyncio.gather(
        first.run("same", lambda: work("one")),
        second.run("same", lambda: work("two")),
    )

    assert results == ["one", "two"]
    assert peak == 1
    assert order in (
        ["one:start", "one:end", "two:start", "two:end"],
        ["two:start", "two:end", "one:start", "one:end"],
    )


@pytest.mark.asyncio
async def test_distinct_conversations_can_run_in_parallel() -> None:
    manager = LeaseManager(MemoryLeaseBackend(), ttl_ms=200)
    entered = asyncio.Event()
    both = asyncio.Event()
    active = 0

    async def work() -> None:
        nonlocal active
        active += 1
        if active == 1:
            entered.set()
        if active == 2:
            both.set()
        await both.wait()
        active -= 1

    first = asyncio.create_task(manager.run("one", work))
    await entered.wait()
    second = asyncio.create_task(manager.run("two", work))
    await asyncio.wait_for(both.wait(), timeout=0.2)
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_non_owner_cannot_renew_or_release() -> None:
    backend = MemoryLeaseBackend()

    assert await backend.acquire("key", "owner", ttl_ms=1_000)
    assert not await backend.renew("key", "intruder", ttl_ms=1_000)
    assert not await backend.release("key", "intruder")
    assert await backend.renew("key", "owner", ttl_ms=1_000)
    assert await backend.release("key", "owner")


@pytest.mark.asyncio
async def test_suppressed_cancellation_cannot_commit_after_lease_loss() -> None:
    backend = LostLeaseBackend()
    manager = LeaseManager(backend, ttl_ms=30)

    async def suppress_cancellation() -> str:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return "must not escape"
        return "unreachable"

    with pytest.raises(LeaseLostError):
        await manager.run("same", suppress_cancellation)
    assert backend.released is True
