"""Persistence sink for secret-safe model call attempts."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.infrastructure.model_routing import ModelCallAttempt
from mybot.repositories import llm_call_log_table


@dataclass(slots=True)
class LlmCallLogRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def record(self, attempt: ModelCallAttempt) -> None:
        conversation_id = (
            UUID(attempt.conversation_id) if attempt.conversation_id is not None else None
        )
        profile_id = UUID(attempt.profile_id) if attempt.profile_id is not None else None
        persona_version_id = (
            UUID(attempt.persona_version_id)
            if attempt.persona_version_id is not None
            else None
        )
        async with self.sessions() as session:
            await session.execute(
                llm_call_log_table.insert().values(
                    id=uuid4(),
                    conversation_id=conversation_id,
                    purpose=attempt.purpose.value,
                    channel=attempt.channel,
                    model=attempt.model,
                    status=attempt.status,
                    prompt_tokens=attempt.prompt_tokens,
                    completion_tokens=attempt.completion_tokens,
                    latency_ms=attempt.latency_ms,
                    input_price_per_million=attempt.input_price_per_million,
                    output_price_per_million=attempt.output_price_per_million,
                    cost_usd_micros=attempt.cost_usd_micros,
                    error_code=attempt.error_code,
                    profile_id=profile_id,
                    persona_version_id=persona_version_id,
                )
            )
            await session.commit()
