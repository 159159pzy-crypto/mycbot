"""Standing tool approvals sourced from operator config in system_kv."""

from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

APPROVALS_KEY = "tools.approved_ids"


class ApprovalConfig(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


@dataclass(slots=True)
class SystemKvApprovals:
    """TTL-cached view of the operator-approved tool ids."""

    config: ApprovalConfig
    ttl_seconds: float = 10.0
    clock: Callable[[], float] = field(default=monotonic)
    _cache: frozenset[str] = field(default=frozenset(), init=False)
    _fetched_at: float | None = field(default=None, init=False)

    async def is_approved(self, tool_id: str) -> bool:
        now = self.clock()
        if self._fetched_at is None or now - self._fetched_at >= self.ttl_seconds:
            self._cache = await self._load()
            self._fetched_at = now
        return tool_id in self._cache

    async def _load(self) -> frozenset[str]:
        value = await self.config.get(APPROVALS_KEY)
        if isinstance(value, list):
            return frozenset(str(item) for item in value)  # pyright: ignore[reportUnknownArgumentType]
        return frozenset()
