"""Knowledge-base documents, hierarchical chunks, and annotation replies."""

from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr


class KnowledgeScope(StrEnum):
    GLOBAL = "GLOBAL"
    CONVERSATION = "CONVERSATION"


class KnowledgeSourceType(StrEnum):
    MARKDOWN = "md"
    TEXT = "txt"
    PDF = "pdf"


class KnowledgeDocumentStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class KnowledgeChunkLevel(StrEnum):
    PARENT = "PARENT"
    CHILD = "CHILD"


class KnowledgeDocument(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    title: NonEmptyStr
    source_type: KnowledgeSourceType
    scope: KnowledgeScope = KnowledgeScope.GLOBAL
    conversation_id: UUID | None = None
    original_filename: NonEmptyStr
    content_hash: NonEmptyStr
    generation: int = Field(default=1, ge=1)
    status: KnowledgeDocumentStatus = KnowledgeDocumentStatus.QUEUED

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope is KnowledgeScope.GLOBAL and self.conversation_id is not None:
            raise ValueError("global knowledge cannot have conversation_id")
        if self.scope is KnowledgeScope.CONVERSATION and self.conversation_id is None:
            raise ValueError("conversation knowledge requires conversation_id")
        return self


class KnowledgeIngestTask(FrozenModel):
    document_id: UUID
    generation: int = Field(ge=1)


class KnowledgeSearchHit(FrozenModel):
    child_id: UUID
    parent_id: UUID
    document_id: UUID
    document_title: NonEmptyStr
    child_content: NonEmptyStr
    parent_content: NonEmptyStr
    score: float = Field(ge=0.0, le=1.0)
    scope: KnowledgeScope


class AnnotationReply(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    scope: KnowledgeScope = KnowledgeScope.GLOBAL
    conversation_id: UUID | None = None
    question: NonEmptyStr
    answer: NonEmptyStr
    threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    enabled: bool = True
    source_message_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope is KnowledgeScope.GLOBAL and self.conversation_id is not None:
            raise ValueError("global annotation cannot have conversation_id")
        if self.scope is KnowledgeScope.CONVERSATION and self.conversation_id is None:
            raise ValueError("conversation annotation requires conversation_id")
        return self


class AnnotationMatch(FrozenModel):
    annotation_id: UUID
    answer: NonEmptyStr
    score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)


__all__ = [
    "AnnotationMatch",
    "AnnotationReply",
    "KnowledgeChunkLevel",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
    "KnowledgeIngestTask",
    "KnowledgeScope",
    "KnowledgeSearchHit",
    "KnowledgeSourceType",
]
