"""Built-in tool for progressive disclosure of Markdown skills."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from mybot.contracts import ToolContext, ToolError, ToolResult, ToolRisk, ToolSpec
from mybot.engine.prompt import estimate_tokens
from mybot.skills import SkillStore


@dataclass(slots=True)
class LoadSkillTool:
    store: SkillStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate(
            {
                "id": "load_skill",
                "description": "Load the complete instructions for one enabled Markdown skill",
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "read_only": True,
                "idempotent": True,
                "risk": ToolRisk.NONE,
                "capabilities": [],
                "approval_required": False,
            }
        )

    async def run(
        self, context: ToolContext, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        del context
        name = arguments.get("name")
        skill = self.store.get(name, include_disabled=False) if isinstance(name, str) else None
        if skill is None:
            return ToolResult.failure(
                ToolError(
                    code="skill_unavailable",
                    message="the requested skill does not exist or is disabled",
                )
            )
        return ToolResult.success(
            {
                "name": skill.name,
                "content": skill.content,
                "estimated_tokens": estimate_tokens(skill.content),
            }
        )


__all__ = ["LoadSkillTool"]
