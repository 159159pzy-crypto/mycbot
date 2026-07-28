from pathlib import Path
from uuid import UUID

import pytest

from mybot.contracts import ChatKind, ConversationKey, ToolContext
from mybot.skills import SkillStore
from mybot.tools.skills import LoadSkillTool


@pytest.mark.asyncio
async def test_load_skill_returns_enabled_full_document(tmp_path: Path) -> None:
    folder = tmp_path / "daily-summary"
    folder.mkdir()
    folder.joinpath("SKILL.md").write_text(
        "---\n"
        "name: daily-summary\n"
        "description: Daily format\n"
        "trigger: daily summary\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    store = SkillStore(tmp_path)
    tool = LoadSkillTool(store)
    context = ToolContext(
        invocation_id=UUID("00000000-0000-0000-0000-000000000001"),
        conversation=ConversationKey(
            connection_id="telegram-main",
            chat_kind=ChatKind.DIRECT,
            chat_id="42",
        ),
        actor_identity_id="telegram:42",
        correlation_id="trace-1",
    )
    result = await tool.run(
        context,
        {"name": "daily-summary"},
    )
    assert result.ok is True
    assert result.data is not None
    assert "Body" in result.data["content"]
    assert result.data["estimated_tokens"] > 0

    store.set_enabled("daily-summary", False)
    refused = await tool.run(
        context,
        {"name": "daily-summary"},
    )
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "skill_unavailable"
