from uuid import uuid4

import pytest
from pydantic import ValidationError

from mybot.contracts import AnnotationReply, KnowledgeDocument, KnowledgeScope


def test_conversation_knowledge_requires_owner() -> None:
    with pytest.raises(ValidationError):
        KnowledgeDocument(
            title="FAQ",
            source_type="md",
            scope=KnowledgeScope.CONVERSATION,
            original_filename="faq.md",
            content_hash="abc",
        )


def test_global_annotation_forbids_conversation_owner() -> None:
    with pytest.raises(ValidationError):
        AnnotationReply(
            scope=KnowledgeScope.GLOBAL,
            conversation_id=uuid4(),
            question="退款规则?",
            answer="请联系管理员。",
        )
