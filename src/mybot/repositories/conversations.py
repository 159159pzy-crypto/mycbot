"""Conversation persistence keyed by the stable conversation identity."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import ConversationKey, Platform
from mybot.repositories import conversations_table


@dataclass(slots=True, frozen=True)
class ConversationRecord:
    id: UUID
    stable_key: str


@dataclass(slots=True, frozen=True)
class ConversationDetail:
    id: UUID
    stable_key: str
    connection_id: str
    platform: str
    chat_kind: str
    chat_id: str
    ephemeral: bool = False


@dataclass(slots=True)
class ConversationRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def get_or_create(
        self, key: ConversationKey, *, platform: Platform, ephemeral: bool = False
    ) -> ConversationRecord:
        """Idempotently resolve the row for a conversation key, upsert-safe under races."""

        async with self.sessions() as session:
            insert = (
                pg_insert(conversations_table)
                .values(
                    id=uuid4(),
                    stable_key=key.stable_key,
                    connection_id=key.connection_id,
                    platform=platform.value,
                    chat_kind=key.chat_kind.value,
                    chat_id=key.chat_id,
                    thread_id=key.thread_id,
                    ephemeral=ephemeral or platform is Platform.SANDBOX,
                )
                .on_conflict_do_nothing(index_elements=["stable_key"])
            )
            await session.execute(insert)
            await session.commit()
            row = (
                await session.execute(
                    sa.select(
                        conversations_table.c.id, conversations_table.c.stable_key
                    ).where(conversations_table.c.stable_key == key.stable_key)
                )
            ).one()
            return ConversationRecord(id=row.id, stable_key=row.stable_key)

    async def by_stable_key(self, stable_key: str) -> ConversationDetail | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.select(
                        conversations_table.c.id,
                        conversations_table.c.stable_key,
                        conversations_table.c.connection_id,
                        conversations_table.c.platform,
                        conversations_table.c.chat_kind,
                        conversations_table.c.chat_id,
                        conversations_table.c.ephemeral,
                    ).where(conversations_table.c.stable_key == stable_key)
                )
            ).one_or_none()
            if row is None:
                return None
            return ConversationDetail(
                id=row.id,
                stable_key=row.stable_key,
                connection_id=row.connection_id,
                platform=row.platform,
                chat_kind=row.chat_kind,
                chat_id=row.chat_id,
                ephemeral=bool(row.ephemeral),
            )
