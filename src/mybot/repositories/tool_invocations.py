"""Tool invocation audit persistence, one row per executed tool call."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.repositories import tool_invocations_table


@dataclass(slots=True, frozen=True)
class ToolInvocationRecord:
    tool_id: str
    ok: bool
    error_code: str | None
    latency_ms: int


@dataclass(slots=True)
class ToolInvocationRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record_many(
        self, turn_id: UUID, invocations: Sequence[ToolInvocationRecord]
    ) -> None:
        if not invocations:
            return
        async with self.sessions() as session:
            await session.execute(
                insert(tool_invocations_table),
                [
                    {
                        "id": uuid4(),
                        "turn_id": turn_id,
                        "tool_id": invocation.tool_id,
                        "ok": invocation.ok,
                        "error_code": invocation.error_code,
                        "latency_ms": invocation.latency_ms,
                    }
                    for invocation in invocations
                ],
            )
            await session.commit()
