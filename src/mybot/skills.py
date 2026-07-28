"""File-backed Markdown skills with bounded prompt disclosure."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mybot.contracts.json import parse_json

_NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_STATE_FILE = ".mybot-skills.json"


@dataclass(slots=True, frozen=True)
class SkillDocument:
    name: str
    description: str
    trigger: str
    body: str
    content: str
    path: Path
    enabled: bool


class SkillStore:
    """Discovers and edits SKILL.md files without allowing root escape."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def list(self) -> tuple[SkillDocument, ...]:
        if not self.root.exists():
            return ()
        enabled = self._enabled_state()
        found: dict[str, SkillDocument] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            if path.is_symlink() or not self._contained(path):
                continue
            try:
                content = path.read_text(encoding="utf-8")
                metadata, body = _parse_document(content)
            except (OSError, UnicodeError, ValueError):
                continue
            name = metadata["name"]
            if name in found:
                continue
            found[name] = SkillDocument(
                name=name,
                description=metadata["description"],
                trigger=metadata["trigger"],
                body=body,
                content=content,
                path=path,
                enabled=enabled.get(name, True),
            )
        return tuple(found[name] for name in sorted(found))

    def get(self, name: str, *, include_disabled: bool = True) -> SkillDocument | None:
        for skill in self.list():
            if skill.name == name and (include_disabled or skill.enabled):
                return skill
        return None

    def prompt_catalog(self, *, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        prefix = (
            "Available skills. Use load_skill with a skill name only when its full "
            "instructions are relevant:\n<available_skills>\n"
        )
        suffix = "</available_skills>"
        if len(prefix) + len(suffix) > max_chars:
            return (prefix + suffix)[:max_chars]
        lines: list[str] = []
        used = len(prefix) + len(suffix)
        for skill in self.list():
            if not skill.enabled:
                continue
            line = (
                f'<skill name="{_xml(skill.name)}" description="{_xml(skill.description)}" '
                f'trigger="{_xml(skill.trigger)}" />\n'
            )
            if used + len(line) > max_chars:
                line = f'<skill name="{_xml(skill.name)}" />\n'
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        return prefix + "".join(lines) + suffix

    def set_enabled(self, name: str, enabled: bool) -> SkillDocument:
        skill = self.get(name)
        if skill is None:
            raise KeyError(name)
        state = self._enabled_state()
        state[name] = enabled
        self._write_state(state)
        refreshed = self.get(name)
        assert refreshed is not None
        return refreshed

    def save(self, name: str, content: str) -> SkillDocument:
        if _NAME.fullmatch(name) is None:
            raise ValueError("skill name must be a stable identifier")
        metadata, _body = _parse_document(content)
        if metadata["name"] != name:
            raise ValueError("frontmatter name must match the skill directory")
        folder = (self.root / name).resolve()
        if not self._contained(folder):
            raise ValueError("skill path escapes the skills root")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / "SKILL.md"
        temporary = folder / ".SKILL.md.tmp"
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(destination)
        skill = self.get(name)
        if skill is None:
            raise ValueError("saved skill could not be loaded")
        return skill

    def _contained(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def _enabled_state(self) -> dict[str, bool]:
        path = self.root / _STATE_FILE
        try:
            decoded = parse_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(decoded, dict):
            return {}
        return {str(key): value for key, value in decoded.items() if isinstance(value, bool)}

    def _write_state(self, state: dict[str, bool]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / _STATE_FILE
        temporary = self.root / f"{_STATE_FILE}.tmp"
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)


def _parse_document(content: str) -> tuple[dict[str, str], str]:
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("SKILL.md requires YAML frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter is not terminated")
    metadata: dict[str, str] = {}
    for raw_line in normalized[4:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError("SKILL.md frontmatter must use key: value lines")
        metadata[key.strip()] = value.strip().strip('"\'')
    for key in ("name", "description", "trigger"):
        if not metadata.get(key):
            raise ValueError(f"SKILL.md frontmatter requires {key}")
    if _NAME.fullmatch(metadata["name"]) is None:
        raise ValueError("SKILL.md frontmatter name is invalid")
    return metadata, normalized[end + 5 :].lstrip("\n")


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = ["SkillDocument", "SkillStore"]
