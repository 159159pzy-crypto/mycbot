"""Turn audit persistence: one row per agent decision outcome."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import TurnDecision
from mybot.repositories import turns_table

TurnOutcome = Literal["replied", "fallback", "budget_exceeded", "error"]


@dataclass(slots=True)
class TurnRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record_turn(
        self,
        conversation_id: UUID,
        decision: TurnDecision,
        *,
        outcome: TurnOutcome,
        inbound_message_id: UUID | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> UUID:
        turn_id = uuid4()
        async with self.sessions() as session:
            await session.execute(
                insert(turns_table).values(
                    id=turn_id,
                    conversation_id=conversation_id,
                    inbound_message_id=inbound_message_id,
                    action=decision.action.value,
                    trigger=decision.trigger.value,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    outcome=outcome,
                    error=error,
                )
            )
            await session.commit()
        return turn_id
