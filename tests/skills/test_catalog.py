from pathlib import Path

import pytest

from mybot.skills import SkillStore


def write_skill(root: Path, slug: str, *, description: str = "Create a daily summary") -> None:
    folder = root / slug
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        f"name: {slug}\n"
        f"description: {description}\n"
        "trigger: when the user asks for a daily summary\n"
        "---\n\n"
        "Use the following three-section format.\n",
        encoding="utf-8",
    )


def test_skill_store_discovers_toggles_edits_and_builds_compact_prompt(tmp_path: Path) -> None:
    write_skill(tmp_path, "daily-summary")
    store = SkillStore(tmp_path)

    skill = store.get("daily-summary")
    assert skill is not None
    assert skill.enabled is True
    assert "three-section" in skill.content
    assert "three-section" not in store.prompt_catalog(max_chars=2_000)
    assert "daily-summary" in store.prompt_catalog(max_chars=2_000)

    store.set_enabled("daily-summary", False)
    assert "daily-summary" not in store.prompt_catalog(max_chars=2_000)
    assert store.get("daily-summary") is not None
    assert store.get("daily-summary").enabled is False  # type: ignore[union-attr]

    store.save(
        "daily-summary",
        "---\nname: daily-summary\ndescription: Revised\ntrigger: summaries\n---\n\nNew body.\n",
    )
    assert store.get("daily-summary").body.strip() == "New body."  # type: ignore[union-attr]


def test_skill_store_rejects_invalid_frontmatter_and_path_escape(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    with pytest.raises(ValueError, match="frontmatter"):
        store.save("bad", "no frontmatter")
    with pytest.raises(ValueError, match="name"):
        store.save("../escape", "---\nname: escape\ndescription: x\ntrigger: x\n---\nx")


def test_skill_prompt_catalog_obeys_budget(tmp_path: Path) -> None:
    for index in range(8):
        write_skill(tmp_path, f"skill-{index}", description="x" * 80)
    prompt = SkillStore(tmp_path).prompt_catalog(max_chars=260)
    assert len(prompt) <= 260
    assert "load_skill" in prompt
