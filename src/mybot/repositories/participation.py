"""Persistence for willingness decisions and proactive generation outcomes."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import WillingnessScore
from mybot.repositories import (
    proactive_generation_audit_table,
    reply_willingness_audit_table,
)


@dataclass(slots=True)
class WillingnessAuditRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self,
        *,
        conversation_id: UUID,
        message_id: UUID | None,
        profile_id: UUID | None,
        score: WillingnessScore,
    ) -> None:
        async with self.sessions() as session:
            await session.execute(
                sa.insert(reply_willingness_audit_table).values(
                    id=uuid4(),
                    conversation_id=conversation_id,
                    message_id=message_id,
                    profile_id=profile_id,
                    score=score.score,
                    threshold=score.threshold,
                    allowed=score.allowed,
                    components=score.components.model_dump(mode="json"),
                    reason=score.reason,
                )
            )
            await session.commit()

    async def metrics(self, *, hours: int = 24) -> dict[str, int]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT allowed, count(*) AS n
                        FROM reply_willingness_audit
                        WHERE created_at >= now() - make_interval(hours => :hours)
                        GROUP BY allowed
                        """
                    ),
                    {"hours": max(1, hours)},
                )
            ).mappings().all()
        counts = {bool(row["allowed"]): int(row["n"]) for row in rows}
        return {"allowed": counts.get(True, 0), "blocked": counts.get(False, 0)}


@dataclass(slots=True)
class ProactiveGenerationAuditRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(
        self,
        *,
        conversation_id: UUID,
        profile_id: UUID | None,
        persona_version_id: UUID | None,
        outcome: str,
        response_text: str | None,
        model: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            await session.execute(
                sa.insert(proactive_generation_audit_table).values(
                    id=uuid4(),
                    conversation_id=conversation_id,
                    profile_id=profile_id,
                    persona_version_id=persona_version_id,
                    outcome=outcome,
                    response_text=response_text,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    error_code=error_code,
                )
            )
            await session.commit()
