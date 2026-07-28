"""Conversation profiles, immutable personas, relationships, and participation scores."""

from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr


class ProfileMemoryPolicy(FrozenModel):
    enabled: bool = True
    retrieval_limit: int = Field(default=5, ge=1, le=50)
    expression_examples: int = Field(default=3, ge=0, le=20)
    relationship_enabled: bool = True


class ReplyWillingnessPolicy(FrozenModel):
    enabled: bool = False
    threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    sensitivity: float = Field(default=1.0, ge=0.5, le=1.5)
    keywords: tuple[NonEmptyStr, ...] = ()


class AgentProfile(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    name: NonEmptyStr
    description: str = ""
    active_persona_version_id: UUID | None = None
    model_tier: NonEmptyStr = "default"
    tool_capabilities: tuple[NonEmptyStr, ...] = ()
    memory: ProfileMemoryPolicy = Field(default_factory=ProfileMemoryPolicy)
    willingness: ReplyWillingnessPolicy = Field(default_factory=ReplyWillingnessPolicy)


class PersonaVersion(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    profile_id: UUID
    version: int = Field(ge=1)
    system_prompt: NonEmptyStr
    parent_version_id: UUID | None = None
    change_note: str = ""


class RelationshipMemory(FrozenModel):
    subject_identity_id: NonEmptyStr
    familiarity: float = Field(default=0.0, ge=0.0, le=100.0)
    impression: NonEmptyStr
    memory_id: UUID | None = None
    version: int = Field(default=1, ge=1)


class WillingnessComponents(FrozenModel):
    keyword: float = Field(default=0.0, ge=0.0, le=1.0)
    question: float = Field(default=0.0, ge=0.0, le=1.0)
    persona_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    group_heat: float = Field(default=0.0, ge=0.0, le=1.0)
    presence_penalty: float = Field(default=0.0, ge=0.0, le=1.0)


class WillingnessScore(FrozenModel):
    score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    allowed: bool
    reason: NonEmptyStr
    components: WillingnessComponents

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.allowed != (self.score >= self.threshold):
            raise ValueError("allowed must match score relative to threshold")
        return self
