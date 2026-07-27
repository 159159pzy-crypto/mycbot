"""Best-effort persistence for bounded, secret-safe completed trace spans."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.repositories import messages_table, trace_spans_table

type TraceStatus = Literal["ok", "error", "skipped"]
_MAX_ATTRIBUTE_TEXT = 2_000


@dataclass(slots=True)
class TraceSpanRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self,
        *,
        trace_id: str,
        stage: str,
        status: TraceStatus = "ok",
        duration_ms: int = 0,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> UUID:
        span_id = uuid4()
        sanitized = _sanitize_attributes(attributes or {})
        async with self.sessions() as session:
            resolved_conversation_id = conversation_id
            if resolved_conversation_id is None and message_id is not None:
                resolved_conversation_id = (
                    await session.execute(
                        sa.select(messages_table.c.conversation_id).where(
                            messages_table.c.id == message_id
                        )
                    )
                ).scalar_one_or_none()
            await session.execute(
                sa.insert(trace_spans_table).values(
                    id=span_id,
                    trace_id=trace_id,
                    conversation_id=resolved_conversation_id,
                    message_id=message_id,
                    stage=stage[:100],
                    status=status,
                    duration_ms=max(0, duration_ms),
                    attributes=sanitized,
                )
            )
            await session.commit()
        return span_id


def _sanitize_attributes(values: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    sanitized: dict[str, JsonValue] = {}
    for key, value in values.items():
        safe_key = str(key)[:100]
        if isinstance(value, str):
            sanitized[safe_key] = value[:_MAX_ATTRIBUTE_TEXT]
        elif isinstance(value, list):
            sanitized[safe_key] = cast(JsonValue, value[:50])
        elif isinstance(value, dict):
            sanitized[safe_key] = cast(JsonValue, dict(list(value.items())[:50]))
        else:
            sanitized[safe_key] = value
    return sanitized
