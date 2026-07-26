"""Operator mutation audit persistence."""

from dataclasses import dataclass
from uuid import uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.repositories import operator_audit_table


@dataclass(slots=True)
class AuditRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(self, action: str, detail: dict[str, JsonValue]) -> None:
        async with self.sessions() as session:
            await session.execute(
                sa.insert(operator_audit_table).values(
                    id=uuid4(), action=action, detail=detail
                )
            )
            await session.commit()

    async def recent(self, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(
                        operator_audit_table.c.action,
                        operator_audit_table.c.detail,
                        operator_audit_table.c.created_at,
                    )
                    .order_by(operator_audit_table.c.created_at.desc())
                    .limit(limit)
                )
            ).mappings().all()
            return [
                {
                    "action": row["action"],
                    "detail": row["detail"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ]
