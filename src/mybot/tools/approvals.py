"""Standing tool approvals sourced from operator config in system_kv."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

from mybot.contracts import ToolContext
from mybot.repositories.safety import ToolApprovalRepository

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


@dataclass(slots=True)
class DatabaseToolApprovals:
    """Persist one invocation and wait for an operator decision."""

    repository: ToolApprovalRepository
    timeout_seconds: float = 60.0
    poll_seconds: float = 0.5

    async def request_approval(
        self,
        tool_id: str,
        context: ToolContext,
        arguments: Mapping[str, JsonValue],
    ) -> str:
        await self.repository.request(
            invocation_id=context.invocation_id,
            tool_id=tool_id,
            conversation_stable_key=context.conversation.stable_key,
            actor_identity_id=context.actor_identity_id,
            correlation_id=context.correlation_id,
            arguments=arguments,
            timeout_seconds=self.timeout_seconds,
        )
        decision = await self.repository.wait(
            context.invocation_id,
            timeout_seconds=self.timeout_seconds,
            poll_seconds=self.poll_seconds,
        )
        return decision.status


__all__ = [
    "APPROVALS_KEY",
    "DatabaseToolApprovals",
    "SystemKvApprovals",
]
