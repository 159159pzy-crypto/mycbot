"""PostgreSQL pairing policies, pending requests, and approvals."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.security.pairing import PairingPolicy, PairingRequest

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(slots=True)
class PairingRepository:
    sessions: async_sessionmaker[AsyncSession]
    request_ttl: timedelta = timedelta(hours=1)
    pending_limit: int = 3

    async def policy(self, platform: str, connection_id: str) -> PairingPolicy:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        SELECT policy, allowlist
                        FROM pairing_policy
                        WHERE platform = :platform AND connection_id = :connection_id
                        """
                    ),
                    {"platform": platform, "connection_id": connection_id},
                )
            ).mappings().one_or_none()
        if row is None:
            return PairingPolicy("open")
        allowlist = row["allowlist"]
        return PairingPolicy(
            str(row["policy"]),
            tuple(str(item) for item in cast(list[object], allowlist))
            if isinstance(allowlist, list)
            else (),
        )

    async def set_policy(
        self,
        platform: str,
        connection_id: str,
        policy: str,
        allowlist: list[str],
    ) -> PairingPolicy:
        if policy not in {"open", "paired", "allowlist"}:
            raise ValueError("policy must be open, paired, or allowlist")
        normalized = sorted({item.strip() for item in allowlist if item.strip()})
        async with self.sessions() as session:
            await session.execute(
                sa.text(
                    """
                    INSERT INTO pairing_policy (platform, connection_id, policy, allowlist)
                    VALUES (:platform, :connection_id, :policy, CAST(:allowlist AS jsonb))
                    ON CONFLICT (platform, connection_id) DO UPDATE
                    SET policy = EXCLUDED.policy,
                        allowlist = EXCLUDED.allowlist,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "platform": platform,
                    "connection_id": connection_id,
                    "policy": policy,
                    "allowlist": __import__("json").dumps(normalized),
                },
            )
            await session.commit()
        return PairingPolicy(policy, tuple(normalized))

    async def approved(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> bool:
        async with self.sessions() as session:
            value = await session.scalar(
                sa.text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pairing_approval
                        WHERE platform = :platform
                          AND connection_id = :connection_id
                          AND subject_identity_id = :subject
                    )
                    """
                ),
                {
                    "platform": platform,
                    "connection_id": connection_id,
                    "subject": subject_identity_id,
                },
            )
            return bool(value)

    async def request(
        self, platform: str, connection_id: str, subject_identity_id: str
    ) -> tuple[PairingRequest | None, bool]:
        now = datetime.now(tz=UTC)
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"pairing:{platform}:{connection_id}"},
            )
            existing = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, platform, connection_id, subject_identity_id, code, expires_at
                        FROM pairing_request
                        WHERE platform = :platform
                          AND connection_id = :connection_id
                          AND subject_identity_id = :subject
                          AND approved_at IS NULL
                          AND dismissed_at IS NULL
                          AND expires_at > :now
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "platform": platform,
                        "connection_id": connection_id,
                        "subject": subject_identity_id,
                        "now": now,
                    },
                )
            ).mappings().one_or_none()
            if existing is not None:
                return _request_from_row(existing), False
            count = await session.scalar(
                sa.text(
                    """
                    SELECT count(*) FROM pairing_request
                    WHERE platform = :platform
                      AND connection_id = :connection_id
                      AND approved_at IS NULL
                      AND dismissed_at IS NULL
                      AND expires_at > :now
                    """
                ),
                {"platform": platform, "connection_id": connection_id, "now": now},
            )
            if int(count or 0) >= self.pending_limit:
                return None, False
            request_id = uuid4()
            code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
            expires = now + self.request_ttl
            await session.execute(
                sa.text(
                    """
                    INSERT INTO pairing_request (
                        id, platform, connection_id, subject_identity_id, code,
                        created_at, expires_at
                    ) VALUES (
                        :id, :platform, :connection_id, :subject, :code, :now, :expires
                    )
                    """
                ),
                {
                    "id": request_id,
                    "platform": platform,
                    "connection_id": connection_id,
                    "subject": subject_identity_id,
                    "code": code,
                    "now": now,
                    "expires": expires,
                },
            )
        return PairingRequest(
            id=str(request_id),
            platform=platform,
            connection_id=connection_id,
            subject_identity_id=subject_identity_id,
            code=code,
            expires_at=expires.isoformat(),
        ), True

    async def pending(self) -> list[dict[str, object]]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, platform, connection_id, subject_identity_id,
                               code, created_at, expires_at
                        FROM pairing_request
                        WHERE approved_at IS NULL
                          AND dismissed_at IS NULL
                          AND expires_at > CURRENT_TIMESTAMP
                        ORDER BY created_at DESC
                        """
                    )
                )
            ).mappings().all()
        return [
            {
                "id": str(row["id"]),
                "platform": row["platform"],
                "connection_id": row["connection_id"],
                "subject_identity_id": row["subject_identity_id"],
                "code": row["code"],
                "created_at": row["created_at"].isoformat(),
                "expires_at": row["expires_at"].isoformat(),
            }
            for row in rows
        ]

    async def approve(self, request_id: UUID) -> bool:
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    sa.text(
                        """
                        UPDATE pairing_request
                        SET approved_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                          AND approved_at IS NULL
                          AND dismissed_at IS NULL
                          AND expires_at > CURRENT_TIMESTAMP
                        RETURNING platform, connection_id, subject_identity_id
                        """
                    ),
                    {"id": request_id},
                )
            ).mappings().one_or_none()
            if row is None:
                return False
            await session.execute(
                sa.text(
                    """
                    INSERT INTO pairing_approval (
                        platform, connection_id, subject_identity_id
                    ) VALUES (:platform, :connection_id, :subject)
                    ON CONFLICT (platform, connection_id, subject_identity_id)
                    DO UPDATE SET approved_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "platform": row["platform"],
                    "connection_id": row["connection_id"],
                    "subject": row["subject_identity_id"],
                },
            )
        return True

    async def dismiss(self, request_id: UUID) -> bool:
        async with self.sessions() as session:
            dismissed = await session.scalar(
                sa.text(
                    """
                    UPDATE pairing_request SET dismissed_at = CURRENT_TIMESTAMP
                    WHERE id = :id AND approved_at IS NULL AND dismissed_at IS NULL
                    RETURNING id
                    """
                ),
                {"id": request_id},
            )
            await session.commit()
            return dismissed is not None


def _request_from_row(row: object) -> PairingRequest:
    values = cast(dict[str, object], row)
    expires = cast(datetime, values["expires_at"])
    return PairingRequest(
        id=str(values["id"]),
        platform=str(values["platform"]),
        connection_id=str(values["connection_id"]),
        subject_identity_id=str(values["subject_identity_id"]),
        code=str(values["code"]),
        expires_at=expires.isoformat(),
    )


__all__ = ["PairingRepository"]
