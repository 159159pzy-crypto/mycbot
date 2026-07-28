"""Message persistence with duplicate reporting and delivery backfill."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import ChatKind, ConversationKey, MessageEnvelope, ReplyPlan
from mybot.engine.willingness import ActivitySnapshot
from mybot.repositories import conversations_table, messages_table

INBOUND = "inbound"
OUTBOUND = "outbound"
SELF_SENDER = "self"


def platform_message_id_from_envelope(envelope: MessageEnvelope) -> str:
    """Extract the platform-native message id from the composite envelope id."""

    return envelope.id.rsplit(":", 1)[-1]


@dataclass(slots=True, frozen=True)
class StoredMessage:
    id: UUID
    duplicate: bool


@dataclass(slots=True, frozen=True)
class MessageText:
    direction: str
    sender: str
    text: str
    id: UUID | None = None


@dataclass(slots=True, frozen=True)
class GroupLearningContext:
    conversation_id: UUID
    conversation: ConversationKey
    source_message_ids: tuple[str, ...]
    messages: tuple[MessageText, ...]


def _text_from_segments(segments: object) -> str:
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for raw in segments:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(raw, dict):
            segment = cast(dict[str, object], raw)
            if segment.get("type") == "text":
                text = segment.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif segment.get("type") == "image":
                alt = segment.get("alt_text")
                parts.append(f"[图片: {alt if isinstance(alt, str) and alt else '图片'}]")
            elif segment.get("type") == "sticker":
                name = segment.get("name")
                parts.append(f"[表情: {name if isinstance(name, str) and name else '表情'}]")
            elif segment.get("type") == "voice":
                parts.append("[语音消息]")
            elif segment.get("type") == "file":
                name = segment.get("name")
                parts.append(f"[文件: {name if isinstance(name, str) and name else '文件'}]")
    return "\n".join(parts)


@dataclass(slots=True)
class MessageRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record_inbound(
        self, conversation_id: UUID, envelope: MessageEnvelope
    ) -> StoredMessage:
        """Persist an inbound envelope; report (not raise) redelivery duplicates."""

        message_id = uuid4()
        segments: list[JsonValue] = [
            segment.model_dump(mode="json") for segment in envelope.segments
        ]
        raw = envelope.model_dump(mode="json").get("raw_ref")
        async with self.sessions() as session:
            insert = (
                pg_insert(messages_table)
                .values(
                    id=message_id,
                    conversation_id=conversation_id,
                    direction=INBOUND,
                    platform_message_id=platform_message_id_from_envelope(envelope),
                    sender_identity_id=envelope.sender_identity_id,
                    segments=segments,
                    occurred_at=envelope.occurred_at,
                    raw=raw,
                    trace_id=envelope.trace_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["conversation_id", "direction", "platform_message_id"]
                )
                .returning(messages_table.c.id)
            )
            inserted = (await session.execute(insert)).scalar_one_or_none()
            if inserted is not None:
                await session.execute(
                    sa.update(conversations_table)
                    .where(conversations_table.c.id == conversation_id)
                    .values(last_message_at=envelope.occurred_at)
                )
            await session.commit()
        if inserted is None:
            async with self.sessions() as session:
                existing = (
                    await session.execute(
                        sa.select(messages_table.c.id).where(
                            messages_table.c.conversation_id == conversation_id,
                            messages_table.c.direction == INBOUND,
                            messages_table.c.platform_message_id
                            == platform_message_id_from_envelope(envelope),
                        )
                    )
                ).scalar_one()
            return StoredMessage(id=existing, duplicate=True)
        return StoredMessage(id=inserted, duplicate=False)

    async def recent_activity(
        self, conversation_id: UUID, *, since: datetime, limit: int = 200
    ) -> ActivitySnapshot:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(messages_table.c.direction)
                    .where(
                        messages_table.c.conversation_id == conversation_id,
                        messages_table.c.occurred_at >= since,
                    )
                    .order_by(messages_table.c.occurred_at.desc())
                    .limit(limit)
                )
            ).all()
        inbound = sum(1 for row in rows if row.direction == INBOUND)
        outbound = sum(1 for row in rows if row.direction == OUTBOUND)
        return ActivitySnapshot(inbound=inbound, outbound=outbound)

    async def record_outbound(
        self,
        conversation_id: UUID,
        plan: ReplyPlan,
        *,
        occurred_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> UUID:
        """Persist a planned reply with its platform message id pending delivery."""

        message_id = uuid4()
        segments: list[JsonValue] = [
            {"type": "text", "text": text} for text in plan.text_segments
        ]
        segments.extend(
            cast(JsonValue, segment.model_dump(mode="json"))
            for segment in plan.media_segments
        )
        async with self.sessions() as session:
            await session.execute(
                sa.insert(messages_table).values(
                    id=message_id,
                    conversation_id=conversation_id,
                    direction=OUTBOUND,
                    platform_message_id=None,
                    sender_identity_id=SELF_SENDER,
                    segments=segments,
                    occurred_at=occurred_at or datetime.now(tz=UTC),
                    trace_id=trace_id,
                )
            )
            await session.commit()
        return message_id

    async def mark_delivered(self, message_id: UUID, platform_message_id: str) -> None:
        async with self.sessions() as session:
            await session.execute(
                sa.update(messages_table)
                .where(messages_table.c.id == message_id)
                .values(platform_message_id=platform_message_id)
            )
            await session.commit()

    async def recent_outbound_platform_ids(
        self, conversation_id: UUID, *, limit: int = 50
    ) -> tuple[str, ...]:
        """Platform ids of recently delivered replies, newest first, for reply detection."""

        async with self.sessions() as session:
            rows = await session.execute(
                sa.select(messages_table.c.platform_message_id)
                .where(
                    messages_table.c.conversation_id == conversation_id,
                    messages_table.c.direction == OUTBOUND,
                    messages_table.c.platform_message_id.is_not(None),
                )
                .order_by(messages_table.c.occurred_at.desc())
                .limit(limit)
            )
            return tuple(str(value) for (value,) in rows if value is not None)

    async def recent_texts(
        self, conversation_id: UUID, *, limit: int = 40
    ) -> tuple[MessageText, ...]:
        """Newest-first text extracts of recent messages for prompt history."""

        async with self.sessions() as session:
            rows = await session.execute(
                sa.select(
                    messages_table.c.id,
                    messages_table.c.direction,
                    messages_table.c.sender_identity_id,
                    messages_table.c.segments,
                )
                .where(messages_table.c.conversation_id == conversation_id)
                .order_by(messages_table.c.occurred_at.desc())
                .limit(limit)
            )
            extracted: list[MessageText] = []
            for message_id, direction, sender, segments in rows:
                text = _text_from_segments(segments)
                if text:
                    extracted.append(
                        MessageText(
                            direction=str(direction),
                            sender=str(sender),
                            text=text,
                            id=message_id,
                        )
                    )
            return tuple(extracted)

    async def stored_segments(self, message_id: UUID) -> JsonValue:
        """Return the stored segment JSON for round-trip verification."""

        async with self.sessions() as session:
            row = await session.execute(
                sa.select(messages_table.c.segments).where(messages_table.c.id == message_id)
            )
            return row.scalar_one()

    async def recent_group_learning_contexts(
        self,
        *,
        since: datetime,
        conversation_limit: int,
        message_limit: int,
    ) -> tuple[GroupLearningContext, ...]:
        """Return bounded inbound group dialogue for expression/relationship learning."""

        async with self.sessions() as session:
            conversations = (
                await session.execute(
                    sa.select(
                        conversations_table.c.id,
                        conversations_table.c.connection_id,
                        conversations_table.c.chat_id,
                        conversations_table.c.thread_id,
                    )
                    .where(
                        conversations_table.c.chat_kind == ChatKind.GROUP.value,
                        conversations_table.c.ephemeral.is_(False),
                        conversations_table.c.last_message_at >= since,
                    )
                    .order_by(conversations_table.c.last_message_at.desc())
                    .limit(conversation_limit)
                )
            ).mappings().all()
            contexts: list[GroupLearningContext] = []
            for conversation in conversations:
                rows = (
                    await session.execute(
                        sa.select(
                            messages_table.c.id,
                            messages_table.c.sender_identity_id,
                            messages_table.c.segments,
                        )
                        .where(
                            messages_table.c.conversation_id == conversation["id"],
                            messages_table.c.direction == INBOUND,
                            messages_table.c.occurred_at >= since,
                        )
                        .order_by(messages_table.c.occurred_at.desc())
                        .limit(message_limit)
                    )
                ).mappings().all()
                extracted: list[MessageText] = []
                for row in reversed(rows):
                    message_text = _text_from_segments(row["segments"])
                    if message_text:
                        extracted.append(
                            MessageText(
                                id=cast(UUID, row["id"]),
                                direction=INBOUND,
                                sender=str(row["sender_identity_id"]),
                                text=message_text,
                            )
                        )
                if len(extracted) < 3:
                    continue
                contexts.append(
                    GroupLearningContext(
                        conversation_id=cast(UUID, conversation["id"]),
                        conversation=ConversationKey(
                            connection_id=str(conversation["connection_id"]),
                            chat_kind=ChatKind.GROUP,
                            chat_id=str(conversation["chat_id"]),
                            thread_id=cast(str | None, conversation["thread_id"]),
                        ),
                        source_message_ids=tuple(
                            str(message.id) for message in extracted if message.id is not None
                        ),
                        messages=tuple(extracted),
                    )
                )
        return tuple(contexts)
