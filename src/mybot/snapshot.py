"""Versioned, secret-free Agent snapshot export and merge import."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import (
    AgentProfile,
    ProfileMemoryPolicy,
    ReplyWillingnessPolicy,
)
from mybot.repositories.core_memory import CoreBlockRepository
from mybot.repositories.profiles import ProfileRepository
from mybot.skills import SkillStore
from mybot.tools.approvals import APPROVALS_KEY

SNAPSHOT_SCHEMA = "mybot.agent.snapshot"
SNAPSHOT_VERSION = 1


class SnapshotProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    model_tier: str = "default"
    tool_capabilities: tuple[str, ...] = ()
    memory: ProfileMemoryPolicy = Field(default_factory=ProfileMemoryPolicy)
    willingness: ReplyWillingnessPolicy = Field(default_factory=ReplyWillingnessPolicy)
    system_prompt: str


class SnapshotCoreBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: Literal["persona", "user_profile"]
    subject_identity_id: str | None = None
    content: str = ""
    token_budget: int = Field(ge=50, le=20_000)


class SnapshotMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    scope: Literal["GLOBAL", "SUBJECT", "CONVERSATION"]
    subject_identity_id: str | None = None
    conversation_stable_key: str | None = None
    kind: str
    content: str
    source_message_ids: tuple[str, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    relationship_score: float | None = Field(default=None, ge=0.0, le=100.0)
    privacy: Literal["PRIVATE", "SHARED", "PUBLIC", "SENSITIVE"]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    conflicts_with: tuple[UUID, ...] = ()
    supersedes: tuple[UUID, ...] = ()
    invalid_at: datetime | None = None
    invalidated_by: UUID | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    created_at: datetime


class SnapshotSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    content: str
    enabled: bool = True


class AgentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["mybot.agent.snapshot"] = SNAPSHOT_SCHEMA
    schema_version: Literal[1] = SNAPSHOT_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    include_history: bool = False
    profiles: tuple[SnapshotProfile, ...] = ()
    core_blocks: tuple[SnapshotCoreBlock, ...] = ()
    memories: tuple[SnapshotMemory, ...] = ()
    skills: tuple[SnapshotSkill, ...] = ()
    approvals: tuple[str, ...] = ()


class SnapshotRepository(Protocol):
    async def export_profiles(self) -> tuple[SnapshotProfile, ...]: ...

    async def export_core_blocks(self) -> tuple[SnapshotCoreBlock, ...]: ...

    async def export_memories(self, *, include_history: bool) -> tuple[SnapshotMemory, ...]: ...

    async def merge_profiles(self, profiles: tuple[SnapshotProfile, ...]) -> int: ...

    async def merge_core_blocks(self, blocks: tuple[SnapshotCoreBlock, ...]) -> int: ...

    async def merge_memories(self, memories: tuple[SnapshotMemory, ...]) -> int: ...


class SnapshotConfig(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...

    async def set(self, key: str, value: JsonValue) -> None: ...


@dataclass(slots=True)
class AgentSnapshotService:
    repository: SnapshotRepository
    skills: SkillStore
    config: SnapshotConfig

    async def export(self, *, include_history: bool = False) -> AgentSnapshot:
        raw_approvals = await self.config.get(APPROVALS_KEY)
        approvals = (
            tuple(sorted(str(item) for item in cast(list[object], raw_approvals)))
            if isinstance(raw_approvals, list)
            else ()
        )
        skill_rows = tuple(
            SnapshotSkill(name=item.name, content=item.content, enabled=item.enabled)
            for item in self.skills.list()
        )
        return AgentSnapshot(
            include_history=include_history,
            profiles=await self.repository.export_profiles(),
            core_blocks=await self.repository.export_core_blocks(),
            memories=await self.repository.export_memories(include_history=include_history),
            skills=skill_rows,
            approvals=approvals,
        )

    async def import_snapshot(self, snapshot: AgentSnapshot) -> dict[str, int]:
        profiles = await self.repository.merge_profiles(snapshot.profiles)
        core_blocks = await self.repository.merge_core_blocks(snapshot.core_blocks)
        memories = await self.repository.merge_memories(snapshot.memories)
        skills = 0
        for item in snapshot.skills:
            self.skills.save(item.name, item.content)
            self.skills.set_enabled(item.name, item.enabled)
            skills += 1
        await self.config.set(APPROVALS_KEY, list(sorted(set(snapshot.approvals))))
        return {
            "profiles": profiles,
            "core_blocks": core_blocks,
            "memories": memories,
            "skills": skills,
            "approvals": len(set(snapshot.approvals)),
        }


@dataclass(slots=True)
class DatabaseSnapshotRepository:
    sessions: async_sessionmaker[AsyncSession]

    async def export_profiles(self) -> tuple[SnapshotProfile, ...]:
        rows = await ProfileRepository(self.sessions).list_all()
        return tuple(
            SnapshotProfile(
                name=row.profile.name,
                description=row.profile.description,
                model_tier=row.profile.model_tier,
                tool_capabilities=row.profile.tool_capabilities,
                memory=row.profile.memory,
                willingness=row.profile.willingness,
                system_prompt=row.persona.system_prompt,
            )
            for row in rows
        )

    async def export_core_blocks(self) -> tuple[SnapshotCoreBlock, ...]:
        rows = await CoreBlockRepository(self.sessions).list_all(limit=10_000)
        return tuple(
            SnapshotCoreBlock(
                label=row.block.label.value,
                subject_identity_id=row.block.subject_identity_id,
                content=row.block.content,
                token_budget=row.block.token_budget,
            )
            for row in rows
        )

    async def export_memories(self, *, include_history: bool) -> tuple[SnapshotMemory, ...]:
        history_clause = "" if include_history else "AND invalid_at IS NULL AND revoked_at IS NULL"
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"""
                        SELECT id, scope, subject_identity_id, conversation_stable_key,
                               kind, content, source_message_ids, confidence,
                               relationship_score, privacy, valid_from, valid_until,
                               conflicts_with, supersedes, invalid_at, invalidated_by,
                               revoked_at, revoked_reason, embedding::text AS embedding,
                               embedding_model, created_at
                        FROM memory_items
                        WHERE 1 = 1 {history_clause}
                        ORDER BY created_at, id
                        """
                    )
                )
            ).mappings().all()
        return tuple(_snapshot_memory(row) for row in rows)

    async def merge_profiles(self, profiles: tuple[SnapshotProfile, ...]) -> int:
        repository = ProfileRepository(self.sessions)
        existing = {row.profile.name: row for row in await repository.list_all()}
        changed = 0
        for item in profiles:
            current = existing.get(item.name)
            if current is None:
                created = await repository.create(
                    AgentProfile(
                        name=item.name,
                        description=item.description,
                        model_tier=item.model_tier,
                        tool_capabilities=item.tool_capabilities,
                        memory=item.memory,
                        willingness=item.willingness,
                    ),
                    system_prompt=item.system_prompt,
                    change_note="snapshot import",
                )
                existing[item.name] = created
                changed += 1
                continue
            profile_changed = (
                current.profile.description != item.description
                or current.profile.model_tier != item.model_tier
                or current.profile.tool_capabilities != item.tool_capabilities
                or current.profile.memory != item.memory
                or current.profile.willingness != item.willingness
            )
            persona_changed = current.persona.system_prompt != item.system_prompt
            if profile_changed:
                updated = current.profile.model_copy(
                    update={
                        "description": item.description,
                        "model_tier": item.model_tier,
                        "tool_capabilities": item.tool_capabilities,
                        "memory": item.memory,
                        "willingness": item.willingness,
                    }
                )
                await repository.update(updated)
            if persona_changed:
                await repository.create_persona_version(
                    current.profile.id,
                    system_prompt=item.system_prompt,
                    change_note="snapshot import",
                )
            if profile_changed or persona_changed:
                changed += 1
        return changed

    async def merge_core_blocks(self, blocks: tuple[SnapshotCoreBlock, ...]) -> int:
        from mybot.contracts import CoreBlockLabel

        repository = CoreBlockRepository(self.sessions)
        existing = {
            (row.block.label.value, row.block.subject_identity_id): row
            for row in await repository.list_all(limit=10_000)
        }
        changed = 0
        for item in blocks:
            current = existing.get((item.label, item.subject_identity_id))
            if (
                current is not None
                and current.block.content == item.content
                and current.block.token_budget == item.token_budget
            ):
                continue
            await repository.replace(
                label=CoreBlockLabel(item.label),
                subject_identity_id=item.subject_identity_id,
                content=item.content,
                token_budget=item.token_budget,
                source="snapshot.import",
            )
            changed += 1
        return changed

    async def merge_memories(self, memories: tuple[SnapshotMemory, ...]) -> int:
        inserted = 0
        async with self.sessions() as session, session.begin():
            imported_ids: set[UUID] = set()
            for item in memories:
                existing = await session.scalar(
                    sa.text(
                        """
                        SELECT id FROM memory_items
                        WHERE scope = :scope
                          AND subject_identity_id IS NOT DISTINCT FROM :subject
                          AND conversation_stable_key IS NOT DISTINCT FROM :conversation
                          AND privacy = :privacy AND kind = :kind AND content = :content
                          AND invalid_at IS NOT DISTINCT FROM :invalid_at
                          AND revoked_at IS NOT DISTINCT FROM :revoked_at
                        LIMIT 1
                        """
                    ),
                    {
                        "scope": item.scope,
                        "subject": item.subject_identity_id,
                        "conversation": item.conversation_stable_key,
                        "privacy": item.privacy,
                        "kind": item.kind,
                        "content": item.content,
                        "invalid_at": item.invalid_at,
                        "revoked_at": item.revoked_at,
                    },
                )
                if existing is not None:
                    continue
                result = await session.execute(
                    sa.text(
                        """
                        INSERT INTO memory_items (
                            id, scope, subject_identity_id, conversation_stable_key,
                            kind, content, source_message_ids, confidence,
                            relationship_score, privacy, valid_from, valid_until,
                            conflicts_with, supersedes, invalid_at, invalidated_by,
                            revoked_at, revoked_reason, embedding, embedding_model, created_at
                        ) VALUES (
                            :id, :scope, :subject, :conversation, :kind, :content,
                            CAST(:sources AS jsonb), :confidence, :relationship_score,
                            :privacy, :valid_from, :valid_until, CAST(:conflicts AS jsonb),
                            CAST(:supersedes AS jsonb), :invalid_at, NULL,
                            :revoked_at, :revoked_reason, CAST(:embedding AS vector),
                            :embedding_model, :created_at
                        )
                        ON CONFLICT (id) DO NOTHING
                        RETURNING id
                        """
                    ),
                    _memory_params(item),
                )
                inserted_id = result.scalar_one_or_none()
                if inserted_id is not None:
                    imported_ids.add(cast(UUID, inserted_id))
                    inserted += 1
            for item in memories:
                if item.id not in imported_ids or item.invalidated_by is None:
                    continue
                await session.execute(
                    sa.text(
                        """
                        UPDATE memory_items SET invalidated_by = :invalidated_by
                        WHERE id = :id
                          AND EXISTS (SELECT 1 FROM memory_items WHERE id = :invalidated_by)
                        """
                    ),
                    {"id": item.id, "invalidated_by": item.invalidated_by},
                )
        return inserted


def read_snapshot(path: Path) -> AgentSnapshot:
    return AgentSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def write_snapshot(path: Path, snapshot: AgentSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        snapshot.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _snapshot_memory(row: object) -> SnapshotMemory:
    values = cast(dict[str, object], row)
    embedding = values.get("embedding")
    vector = (
        tuple(
            float(cast(str | int | float, value))
            for value in cast(list[object], json.loads(embedding))
        )
        if isinstance(embedding, str) and embedding
        else None
    )
    return SnapshotMemory(
        id=cast(UUID, values["id"]),
        scope=cast(Literal["GLOBAL", "SUBJECT", "CONVERSATION"], values["scope"]),
        subject_identity_id=cast(str | None, values["subject_identity_id"]),
        conversation_stable_key=cast(str | None, values["conversation_stable_key"]),
        kind=str(values["kind"]),
        content=str(values["content"]),
        source_message_ids=tuple(
            str(item) for item in cast(list[object], values["source_message_ids"])
        ),
        confidence=float(cast(float, values["confidence"])),
        relationship_score=(
            float(cast(float, values["relationship_score"]))
            if values["relationship_score"] is not None
            else None
        ),
        privacy=cast(Literal["PRIVATE", "SHARED", "PUBLIC", "SENSITIVE"], values["privacy"]),
        valid_from=cast(datetime | None, values["valid_from"]),
        valid_until=cast(datetime | None, values["valid_until"]),
        conflicts_with=tuple(
            UUID(str(item)) for item in cast(list[object], values["conflicts_with"])
        ),
        supersedes=tuple(UUID(str(item)) for item in cast(list[object], values["supersedes"])),
        invalid_at=cast(datetime | None, values["invalid_at"]),
        invalidated_by=cast(UUID | None, values["invalidated_by"]),
        revoked_at=cast(datetime | None, values["revoked_at"]),
        revoked_reason=cast(str | None, values["revoked_reason"]),
        embedding=vector,
        embedding_model=cast(str | None, values["embedding_model"]),
        created_at=cast(datetime, values["created_at"]),
    )


def _memory_params(item: SnapshotMemory) -> dict[str, object]:
    return {
        "id": item.id,
        "scope": item.scope,
        "subject": item.subject_identity_id,
        "conversation": item.conversation_stable_key,
        "kind": item.kind,
        "content": item.content,
        "sources": json.dumps(item.source_message_ids),
        "confidence": item.confidence,
        "relationship_score": item.relationship_score,
        "privacy": item.privacy,
        "valid_from": item.valid_from,
        "valid_until": item.valid_until,
        "conflicts": json.dumps([str(value) for value in item.conflicts_with]),
        "supersedes": json.dumps([str(value) for value in item.supersedes]),
        "invalid_at": item.invalid_at,
        "revoked_at": item.revoked_at,
        "revoked_reason": item.revoked_reason,
        "embedding": (
            "[" + ",".join(f"{value:.8f}" for value in item.embedding) + "]"
            if item.embedding is not None
            else None
        ),
        "embedding_model": item.embedding_model,
        "created_at": item.created_at,
    }


__all__ = [
    "AgentSnapshot",
    "AgentSnapshotService",
    "DatabaseSnapshotRepository",
    "SnapshotCoreBlock",
    "SnapshotMemory",
    "SnapshotProfile",
    "SnapshotSkill",
    "read_snapshot",
    "write_snapshot",
]
