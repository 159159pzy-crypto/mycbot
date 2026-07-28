"""Conversation profile bindings and append-only persona history."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mybot.contracts import (
    AgentProfile,
    PersonaVersion,
    ProfileMemoryPolicy,
    ReplyWillingnessPolicy,
)
from mybot.repositories import (
    agent_profiles_table,
    conversation_profile_bindings_table,
    persona_versions_table,
)

DEFAULT_PROFILE_NAME = "默认"
LEGACY_PERSONA_KEY = "agent.system_prompt"


class LegacyPersonaSource(Protocol):
    async def get(self, key: str) -> JsonValue | None: ...


@dataclass(slots=True, frozen=True)
class ResolvedProfile:
    profile: AgentProfile
    persona: PersonaVersion
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ProfileRepository:
    sessions: async_sessionmaker[AsyncSession]
    legacy_persona: LegacyPersonaSource | None = None

    async def ensure_default(
        self,
        *,
        system_prompt: str,
        tool_capabilities: tuple[str, ...],
    ) -> ResolvedProfile:
        effective_prompt = system_prompt
        if self.legacy_persona is not None:
            legacy = await self.legacy_persona.get(LEGACY_PERSONA_KEY)
            if isinstance(legacy, str) and legacy.strip():
                effective_prompt = legacy.strip()
        profile_id = uuid4()
        async with self.sessions() as session, session.begin():
            await session.execute(
                pg_insert(agent_profiles_table)
                .values(
                    id=profile_id,
                    name=DEFAULT_PROFILE_NAME,
                    description="未绑定会话使用的默认档位",
                    model_tier="default",
                    tool_capabilities=list(tool_capabilities),
                    memory_policy=ProfileMemoryPolicy().model_dump(mode="json"),
                    willingness_policy=ReplyWillingnessPolicy().model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=["name"])
            )
            row = (
                await session.execute(
                    sa.select(agent_profiles_table)
                    .where(agent_profiles_table.c.name == DEFAULT_PROFILE_NAME)
                    .with_for_update()
                )
            ).mappings().one()
            if row["active_persona_version_id"] is None:
                version_id = uuid4()
                await session.execute(
                    sa.insert(persona_versions_table).values(
                        id=version_id,
                        profile_id=row["id"],
                        version=1,
                        system_prompt=effective_prompt.strip(),
                        parent_version_id=None,
                        change_note="initial default persona",
                    )
                )
                await session.execute(
                    sa.update(agent_profiles_table)
                    .where(agent_profiles_table.c.id == row["id"])
                    .values(active_persona_version_id=version_id, updated_at=sa.func.now())
                )
        result = await self.get(cast(UUID, row["id"]))
        assert result is not None
        return result

    async def create(
        self,
        profile: AgentProfile,
        *,
        system_prompt: str,
        change_note: str = "",
    ) -> ResolvedProfile:
        prompt = system_prompt.strip()
        if not prompt:
            raise ValueError("system_prompt must not be blank")
        version_id = uuid4()
        async with self.sessions() as session, session.begin():
            await session.execute(
                sa.insert(agent_profiles_table).values(
                    id=profile.id,
                    name=profile.name,
                    description=profile.description,
                    model_tier=profile.model_tier,
                    tool_capabilities=list(profile.tool_capabilities),
                    memory_policy=profile.memory.model_dump(mode="json"),
                    willingness_policy=profile.willingness.model_dump(mode="json"),
                    active_persona_version_id=None,
                )
            )
            await session.execute(
                sa.insert(persona_versions_table).values(
                    id=version_id,
                    profile_id=profile.id,
                    version=1,
                    system_prompt=prompt,
                    parent_version_id=None,
                    change_note=change_note,
                )
            )
            await session.execute(
                sa.update(agent_profiles_table)
                .where(agent_profiles_table.c.id == profile.id)
                .values(active_persona_version_id=version_id, updated_at=sa.func.now())
            )
        result = await self.get(profile.id)
        assert result is not None
        return result

    async def get(self, profile_id: UUID) -> ResolvedProfile | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    _resolved_profile_select().where(agent_profiles_table.c.id == profile_id)
                )
            ).mappings().one_or_none()
        return _resolved(row) if row is not None else None

    async def list_all(self) -> tuple[ResolvedProfile, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    _resolved_profile_select().order_by(agent_profiles_table.c.name)
                )
            ).mappings().all()
        return tuple(_resolved(row) for row in rows)

    async def resolve(
        self,
        conversation_id: UUID,
        *,
        default_system_prompt: str,
        default_tool_capabilities: tuple[str, ...],
    ) -> ResolvedProfile:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    _resolved_profile_select()
                    .join(
                        conversation_profile_bindings_table,
                        conversation_profile_bindings_table.c.profile_id
                        == agent_profiles_table.c.id,
                    )
                    .where(
                        conversation_profile_bindings_table.c.conversation_id
                        == conversation_id
                    )
                )
            ).mappings().one_or_none()
        if row is not None:
            return _resolved(row)
        return await self.ensure_default(
            system_prompt=default_system_prompt,
            tool_capabilities=default_tool_capabilities,
        )

    async def bind(self, conversation_id: UUID, profile_id: UUID) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                pg_insert(conversation_profile_bindings_table)
                .values(conversation_id=conversation_id, profile_id=profile_id)
                .on_conflict_do_update(
                    index_elements=["conversation_id"],
                    set_={"profile_id": profile_id, "updated_at": sa.func.now()},
                )
            )

    async def unbind(self, conversation_id: UUID) -> bool:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                sa.delete(conversation_profile_bindings_table).where(
                    conversation_profile_bindings_table.c.conversation_id
                    == conversation_id
                )
            )
        return bool(getattr(result, "rowcount", 0))

    async def update(self, profile: AgentProfile) -> ResolvedProfile:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                sa.update(agent_profiles_table)
                .where(agent_profiles_table.c.id == profile.id)
                .values(
                    name=profile.name,
                    description=profile.description,
                    model_tier=profile.model_tier,
                    tool_capabilities=list(profile.tool_capabilities),
                    memory_policy=profile.memory.model_dump(mode="json"),
                    willingness_policy=profile.willingness.model_dump(mode="json"),
                    updated_at=sa.func.now(),
                )
            )
            if not getattr(result, "rowcount", 0):
                raise ValueError("profile not found")
        resolved = await self.get(profile.id)
        assert resolved is not None
        return resolved

    async def create_persona_version(
        self,
        profile_id: UUID,
        *,
        system_prompt: str,
        change_note: str = "",
        parent_version_id: UUID | None = None,
    ) -> PersonaVersion:
        prompt = system_prompt.strip()
        if not prompt:
            raise ValueError("system_prompt must not be blank")
        version_id = uuid4()
        async with self.sessions() as session, session.begin():
            profile = (
                await session.execute(
                    sa.select(agent_profiles_table.c.active_persona_version_id)
                    .where(agent_profiles_table.c.id == profile_id)
                    .with_for_update()
                )
            ).one_or_none()
            if profile is None:
                raise ValueError("profile not found")
            current_max = (
                await session.execute(
                    sa.select(sa.func.coalesce(sa.func.max(persona_versions_table.c.version), 0))
                    .where(persona_versions_table.c.profile_id == profile_id)
                )
            ).scalar_one()
            parent = parent_version_id or profile.active_persona_version_id
            await session.execute(
                sa.insert(persona_versions_table).values(
                    id=version_id,
                    profile_id=profile_id,
                    version=int(current_max) + 1,
                    system_prompt=prompt,
                    parent_version_id=parent,
                    change_note=change_note,
                )
            )
            await session.execute(
                sa.update(agent_profiles_table)
                .where(agent_profiles_table.c.id == profile_id)
                .values(active_persona_version_id=version_id, updated_at=sa.func.now())
            )
        return PersonaVersion(
            id=version_id,
            profile_id=profile_id,
            version=int(current_max) + 1,
            system_prompt=prompt,
            parent_version_id=parent,
            change_note=change_note,
        )

    async def rollback_persona(
        self, profile_id: UUID, *, target_version_id: UUID
    ) -> PersonaVersion:
        async with self.sessions() as session:
            target = (
                await session.execute(
                    sa.select(persona_versions_table).where(
                        persona_versions_table.c.id == target_version_id,
                        persona_versions_table.c.profile_id == profile_id,
                    )
                )
            ).mappings().one_or_none()
        if target is None:
            raise ValueError("persona version not found")
        return await self.create_persona_version(
            profile_id,
            system_prompt=cast(str, target["system_prompt"]),
            change_note=f"rollback to v{target['version']}",
            parent_version_id=target_version_id,
        )

    async def persona_versions(self, profile_id: UUID) -> tuple[PersonaVersion, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(persona_versions_table)
                    .where(persona_versions_table.c.profile_id == profile_id)
                    .order_by(persona_versions_table.c.version.desc())
                )
            ).mappings().all()
        return tuple(_persona(row) for row in rows)

    async def bindings(self) -> tuple[tuple[UUID, UUID], ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    sa.select(
                        conversation_profile_bindings_table.c.conversation_id,
                        conversation_profile_bindings_table.c.profile_id,
                    ).order_by(conversation_profile_bindings_table.c.conversation_id)
                )
            ).all()
        return tuple((row.conversation_id, row.profile_id) for row in rows)

    async def delete(self, profile_id: UUID) -> bool:
        """Delete an unbound non-default profile; persona history cascades with it."""

        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    sa.select(agent_profiles_table.c.name).where(
                        agent_profiles_table.c.id == profile_id
                    )
                )
            ).one_or_none()
            if row is None:
                return False
            if row.name == DEFAULT_PROFILE_NAME:
                raise ValueError("the default profile cannot be deleted")
            bound = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(conversation_profile_bindings_table)
                    .where(conversation_profile_bindings_table.c.profile_id == profile_id)
                )
            ).scalar_one()
            if int(bound) > 0:
                raise ValueError("unbind all conversations before deleting this profile")
            await session.execute(
                sa.delete(agent_profiles_table).where(agent_profiles_table.c.id == profile_id)
            )
        return True


def _resolved_profile_select() -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            *agent_profiles_table.c,
            persona_versions_table.c.id.label("persona_id"),
            persona_versions_table.c.profile_id.label("persona_profile_id"),
            persona_versions_table.c.version.label("persona_version"),
            persona_versions_table.c.system_prompt,
            persona_versions_table.c.parent_version_id,
            persona_versions_table.c.change_note,
        )
        .join(
            persona_versions_table,
            persona_versions_table.c.id == agent_profiles_table.c.active_persona_version_id,
        )
    )


def _resolved(row: sa.RowMapping) -> ResolvedProfile:
    profile = AgentProfile(
        id=cast(UUID, row["id"]),
        name=cast(str, row["name"]),
        description=cast(str, row["description"]),
        active_persona_version_id=cast(UUID, row["active_persona_version_id"]),
        model_tier=cast(str, row["model_tier"]),
        tool_capabilities=tuple(
            str(item) for item in cast(list[JsonValue], row["tool_capabilities"])
        ),
        memory=ProfileMemoryPolicy.model_validate(row["memory_policy"]),
        willingness=ReplyWillingnessPolicy.model_validate(row["willingness_policy"]),
    )
    return ResolvedProfile(
        profile=profile,
        persona=PersonaVersion(
            id=cast(UUID, row["persona_id"]),
            profile_id=cast(UUID, row["persona_profile_id"]),
            version=cast(int, row["persona_version"]),
            system_prompt=cast(str, row["system_prompt"]),
            parent_version_id=cast(UUID | None, row["parent_version_id"]),
            change_note=cast(str, row["change_note"]),
        ),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def _persona(row: sa.RowMapping) -> PersonaVersion:
    return PersonaVersion(
        id=cast(UUID, row["id"]),
        profile_id=cast(UUID, row["profile_id"]),
        version=cast(int, row["version"]),
        system_prompt=cast(str, row["system_prompt"]),
        parent_version_id=cast(UUID | None, row["parent_version_id"]),
        change_note=cast(str, row["change_note"]),
    )
