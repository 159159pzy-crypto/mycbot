"""Operator-editable key-value configuration stored in system_kv."""

from dataclasses import dataclass

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.repositories import system_kv_table


@dataclass(slots=True)
class SystemKvRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def get(self, key: str) -> JsonValue | None:
        async with self.sessions() as session:
            row = await session.execute(
                sa.select(system_kv_table.c.value).where(system_kv_table.c.key == key)
            )
            return row.scalar_one_or_none()

    async def set(self, key: str, value: JsonValue) -> None:
        async with self.sessions() as session:
            statement = pg_insert(system_kv_table).values(key=key, value=value)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": statement.excluded.value, "updated_at": sa.func.now()},
                )
            )
            await session.commit()
