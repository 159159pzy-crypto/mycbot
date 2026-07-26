"""Daily token spend accounting with global and per-conversation ceilings."""

from dataclasses import dataclass
from typing import Protocol

_COUNTER_TTL_SECONDS = 172_800  # 48h: outlives the day it accounts for


class CounterBackend(Protocol):
    async def increment(self, key: str, amount: int, *, ttl_seconds: int) -> int: ...


@dataclass(slots=True)
class TokenBudget:
    """Ceilings of 0 disable that ceiling entirely."""

    backend: CounterBackend
    global_daily_ceiling: int
    conversation_daily_ceiling: int
    key_prefix: str = "mybot:agent:tokens"

    async def allows(self, stable_key: str, *, today: str) -> bool:
        if self.global_daily_ceiling > 0:
            spent = await self.backend.increment(
                self._global_key(today), 0, ttl_seconds=_COUNTER_TTL_SECONDS
            )
            if spent >= self.global_daily_ceiling:
                return False
        if self.conversation_daily_ceiling > 0:
            spent = await self.backend.increment(
                self._conversation_key(stable_key, today),
                0,
                ttl_seconds=_COUNTER_TTL_SECONDS,
            )
            if spent >= self.conversation_daily_ceiling:
                return False
        return True

    async def consume(self, stable_key: str, tokens: int, *, today: str) -> None:
        if tokens <= 0:
            return
        await self.backend.increment(
            self._global_key(today), tokens, ttl_seconds=_COUNTER_TTL_SECONDS
        )
        await self.backend.increment(
            self._conversation_key(stable_key, today),
            tokens,
            ttl_seconds=_COUNTER_TTL_SECONDS,
        )

    def _global_key(self, today: str) -> str:
        return f"{self.key_prefix}:{today}:global"

    def _conversation_key(self, stable_key: str, today: str) -> str:
        return f"{self.key_prefix}:{today}:conversation:{stable_key}"
