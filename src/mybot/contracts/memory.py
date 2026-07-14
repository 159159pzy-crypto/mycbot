"""Scoped, attributable, and revocable memory contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, ValidationInfo, field_validator, model_validator

from mybot.contracts.common import FrozenModel, NonEmptyStr, require_utc
from mybot.contracts.conversation import ConversationKey


class MemoryScope(StrEnum):
    GLOBAL = "GLOBAL"
    SUBJECT = "SUBJECT"
    CONVERSATION = "CONVERSATION"


class MemoryPrivacy(StrEnum):
    PRIVATE = "PRIVATE"
    SHARED = "SHARED"
    PUBLIC = "PUBLIC"
    SENSITIVE = "SENSITIVE"


class MemoryItem(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    scope: MemoryScope
    subject_identity_id: NonEmptyStr | None = None
    conversation: ConversationKey | None = None
    kind: NonEmptyStr
    content: NonEmptyStr
    source_message_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    privacy: MemoryPrivacy
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    conflicts_with: tuple[UUID, ...] = ()
    supersedes: tuple[UUID, ...] = ()
    revoked_at: datetime | None = None
    revoked_reason: NonEmptyStr | None = None

    @field_validator("valid_from", "valid_until", "revoked_at")
    @classmethod
    def validate_timestamps(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return require_utc(value, field_name=info.field_name or "memory timestamp")

    @model_validator(mode="after")
    def validate_scope_and_validity(self) -> Self:
        if self.scope is MemoryScope.GLOBAL:
            if self.subject_identity_id is not None or self.conversation is not None:
                raise ValueError("GLOBAL scope forbids subject and conversation references")
        elif self.scope is MemoryScope.SUBJECT:
            if self.subject_identity_id is None:
                raise ValueError("SUBJECT scope requires subject_identity_id")
            if self.conversation is not None:
                raise ValueError("SUBJECT scope forbids a conversation reference")
        elif self.scope is MemoryScope.CONVERSATION:
            if self.conversation is None:
                raise ValueError("CONVERSATION scope requires a conversation reference")
            if self.subject_identity_id is not None:
                raise ValueError("CONVERSATION scope forbids subject_identity_id")

        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be later than valid_from")
        if (self.revoked_at is None) != (self.revoked_reason is None):
            raise ValueError("revoked_at and revoked_reason must be set together")
        if self.id in self.conflicts_with or self.id in self.supersedes:
            raise ValueError("a memory item cannot conflict with or supersede itself")
        return self
