from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from mybot.skills import SkillStore
from mybot.snapshot import (
    AgentSnapshotService,
    SnapshotCoreBlock,
    SnapshotMemory,
    SnapshotProfile,
    read_snapshot,
    write_snapshot,
)


class FakeRepository:
    def __init__(self) -> None:
        self.profile = SnapshotProfile(name="default", system_prompt="你是 MyBot")
        self.block = SnapshotCoreBlock(
            label="persona", content="友好", token_budget=400
        )
        self.memory = SnapshotMemory(
            id=uuid4(),
            scope="GLOBAL",
            kind="FACT",
            content="喜欢简洁回答",
            source_message_ids=("message-1",),
            confidence=0.9,
            privacy="PRIVATE",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
        )
        self.imported: dict[str, int] = {}

    async def export_profiles(self) -> tuple[SnapshotProfile, ...]:
        return (self.profile,)

    async def export_core_blocks(self) -> tuple[SnapshotCoreBlock, ...]:
        return (self.block,)

    async def export_memories(self, *, include_history: bool) -> tuple[SnapshotMemory, ...]:
        assert include_history is True
        return (self.memory,)

    async def merge_profiles(self, profiles: tuple[SnapshotProfile, ...]) -> int:
        self.imported["profiles"] = len(profiles)
        return len(profiles)

    async def merge_core_blocks(self, blocks: tuple[SnapshotCoreBlock, ...]) -> int:
        self.imported["core_blocks"] = len(blocks)
        return len(blocks)

    async def merge_memories(self, memories: tuple[SnapshotMemory, ...]) -> int:
        self.imported["memories"] = len(memories)
        return len(memories)


class FakeConfig:
    def __init__(self) -> None:
        self.values: dict[str, object] = {"tools.approved_ids": ["memory_append"]}

    async def get(self, key: str) -> object:
        return self.values.get(key)

    async def set(self, key: str, value: object) -> None:
        self.values[key] = value


def skill_text() -> str:
    return """---
name: daily-summary
description: Daily summary format
trigger: When a daily summary is requested
---
Use concise bullets.
"""


@pytest.mark.asyncio
async def test_snapshot_round_trip_contains_portable_state_but_no_secret_fields(
    tmp_path: Path,
) -> None:
    skills = SkillStore(tmp_path / "skills")
    skills.save("daily-summary", skill_text())
    service = AgentSnapshotService(FakeRepository(), skills, FakeConfig())

    snapshot = await service.export(include_history=True)
    path = tmp_path / "agent.json"
    write_snapshot(path, snapshot)
    restored = read_snapshot(path)

    assert restored.schema_name == "mybot.agent.snapshot"
    assert restored.schema_version == 1
    assert restored.profiles[0].system_prompt == "你是 MyBot"
    assert restored.memories[0].content == "喜欢简洁回答"
    assert restored.skills[0].name == "daily-summary"
    serialized = path.read_text(encoding="utf-8")
    assert "api_key" not in serialized
    assert "bot_token" not in serialized
    assert "database_url" not in serialized


@pytest.mark.asyncio
async def test_import_merges_database_state_skills_and_approvals(tmp_path: Path) -> None:
    repository = FakeRepository()
    source_skills = SkillStore(tmp_path / "source")
    source_skills.save("daily-summary", skill_text())
    source = AgentSnapshotService(repository, source_skills, FakeConfig())
    snapshot = await source.export(include_history=True)

    target_repository = FakeRepository()
    target_config = FakeConfig()
    target_skills = SkillStore(tmp_path / "target")
    target = AgentSnapshotService(target_repository, target_skills, target_config)

    result = await target.import_snapshot(snapshot)

    assert result == {
        "profiles": 1,
        "core_blocks": 1,
        "memories": 1,
        "skills": 1,
        "approvals": 1,
    }
    assert target_skills.get("daily-summary") is not None
    assert target_config.values["tools.approved_ids"] == ["memory_append"]
