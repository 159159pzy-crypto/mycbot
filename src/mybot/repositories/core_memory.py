"""Bounded, versioned core-memory blocks for persona and user profiles."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import CoreBlock, CoreBlockLabel
from mybot.engine.prompt import estimate_tokens


class CoreBlockLimitError(ValueError):
    """Raised when a core block would exceed its configured token budget."""


@dataclass(slots=True, frozen=True)
class CoreBlockRecord:
    block: CoreBlock
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class CoreBlockRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def visible_for(self, subject_identity_id: str) -> tuple[CoreBlockRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, label, subject_identity_id, content, token_budget,
                               version, created_at, updated_at
                        FROM core_blocks
                        WHERE (label = 'persona' AND subject_identity_id IS NULL)
                           OR (label = 'user_profile' AND subject_identity_id = :subject)
                        ORDER BY CASE label WHEN 'persona' THEN 0 ELSE 1 END
                        """
                    ),
                    {"subject": subject_identity_id},
                )
            ).mappings().all()
            return tuple(_record(row) for row in rows)

    async def list_all(self, *, limit: int = 200) -> tuple[CoreBlockRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, label, subject_identity_id, content, token_budget,
                               version, created_at, updated_at
                        FROM core_blocks
                        ORDER BY label, subject_identity_id NULLS FIRST
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
            return tuple(_record(row) for row in rows)

    async def append(
        self,
        *,
        label: CoreBlockLabel,
        subject_identity_id: str | None,
        content: str,
        token_budget: int,
        source: str,
    ) -> CoreBlockRecord:
        addition = content.strip()
        if not addition:
            raise ValueError("core-memory append content must not be blank")
        return await self._write(
            label=label,
            subject_identity_id=subject_identity_id,
            token_budget=token_budget,
            source=source,
            operation="CORE_APPEND",
            replace_budget=False,
            transform=lambda current: "\n".join(part for part in (current, addition) if part),
        )

    async def replace(
        self,
        *,
        label: CoreBlockLabel,
        subject_identity_id: str | None,
        content: str,
        token_budget: int,
        source: str,
    ) -> CoreBlockRecord:
        replacement = content.strip()
        return await self._write(
            label=label,
            subject_identity_id=subject_identity_id,
            token_budget=token_budget,
            source=source,
            operation="CORE_REPLACE",
            replace_budget=True,
            transform=lambda _current: replacement,
        )

    async def _write(
        self,
        *,
        label: CoreBlockLabel,
        subject_identity_id: str | None,
        token_budget: int,
        source: str,
        operation: str,
        replace_budget: bool,
        transform: Callable[[str], str],
    ) -> CoreBlockRecord:
        owner = _validated_owner(label, subject_identity_id)
        async with self.sessions() as session:
            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
                {"owner": owner},
            )
            row = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, label, subject_identity_id, content, token_budget,
                               version, created_at, updated_at
                        FROM core_blocks
                        WHERE label = :label
                          AND subject_identity_id IS NOT DISTINCT FROM :subject
                        FOR UPDATE
                        """
                    ),
                    {"label": label.value, "subject": subject_identity_id},
                )
            ).mappings().one_or_none()
            current = "" if row is None else str(row["content"])
            resolved_budget = (
                token_budget
                if row is None or replace_budget
                else int(row["token_budget"])
            )
            next_content = transform(current)
            used_tokens = estimate_tokens(next_content) if next_content else 0
            if used_tokens > resolved_budget:
                raise CoreBlockLimitError(
                    f"core block uses {used_tokens} tokens; budget is {resolved_budget}"
                )
            if row is None:
                block = CoreBlock(
                    label=label,
                    subject_identity_id=subject_identity_id,
                    content=next_content,
                    token_budget=resolved_budget,
                )
                result = (
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO core_blocks (
                                id, label, subject_identity_id, content, token_budget, version
                            ) VALUES (
                                :id, :label, :subject, :content, :token_budget, 1
                            )
                            RETURNING id, label, subject_identity_id, content, token_budget,
                                      version, created_at, updated_at
                            """
                        ),
                        {
                            "id": block.id,
                            "label": label.value,
                            "subject": subject_identity_id,
                            "content": next_content,
                            "token_budget": resolved_budget,
                        },
                    )
                ).mappings().one()
            else:
                result = (
                    await session.execute(
                        sa.text(
                            """
                            UPDATE core_blocks
                            SET content = :content, token_budget = :token_budget,
                                version = version + 1, updated_at = now()
                            WHERE id = :id
                            RETURNING id, label, subject_identity_id, content, token_budget,
                                      version, created_at, updated_at
                            """
                        ),
                        {
                            "id": row["id"],
                            "content": next_content,
                            "token_budget": resolved_budget,
                        },
                    )
                ).mappings().one()
            await _audit_core(
                session,
                operation=operation,
                source=source,
                block_id=cast(UUID, result["id"]),
                subject_identity_id=subject_identity_id,
                detail={
                    "label": label.value,
                    "version": int(result["version"]),
                    "token_budget": resolved_budget,
                    "used_tokens": used_tokens,
                    "content_length": len(next_content),
                },
            )
            await session.commit()
            return _record(result)


async def _audit_core(
    session: AsyncSession,
    *,
    operation: str,
    source: str,
    block_id: UUID,
    subject_identity_id: str | None,
    detail: dict[str, object],
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO memory_operation_audit (
                id, operation, source, memory_id, subject_identity_id, detail
            ) VALUES (
                :id, :operation, :source, :memory_id, :subject, CAST(:detail AS jsonb)
            )
            """
        ),
        {
            "id": uuid4(),
            "operation": operation,
            "source": source,
            "memory_id": block_id,
            "subject": subject_identity_id,
            "detail": json.dumps(detail),
        },
    )


def _validated_owner(label: CoreBlockLabel, subject_identity_id: str | None) -> str:
    CoreBlock(
        label=label,
        subject_identity_id=subject_identity_id,
        content="",
        token_budget=50,
    )
    return f"{label.value}:{subject_identity_id or '-'}"


def _record(row: RowMapping) -> CoreBlockRecord:
    return CoreBlockRecord(
        block=CoreBlock(
            id=cast(UUID, row["id"]),
            label=CoreBlockLabel(str(row["label"])),
            subject_identity_id=cast(str | None, row["subject_identity_id"]),
            content=str(row["content"]),
            token_budget=int(row["token_budget"]),
            version=int(row["version"]),
        ),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


__all__ = ["CoreBlockLimitError", "CoreBlockRecord", "CoreBlockRepository"]
