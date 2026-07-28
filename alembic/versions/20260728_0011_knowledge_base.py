"""Add hierarchical knowledge documents and annotation replies.

Revision ID: 20260728_0011
Revises: 20260728_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_0011"
down_revision: str | None = "20260728_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("source_content", sa.LargeBinary(), nullable=False),
        sa.Column("raw_text", sa.Text()),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("embedding_model", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("source_type IN ('md', 'txt', 'pdf')", name="ck_kb_document_type"),
        sa.CheckConstraint(
            "scope IN ('GLOBAL', 'CONVERSATION')", name="ck_kb_document_scope"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_kb_document_status",
        ),
        sa.CheckConstraint(
            "(scope = 'GLOBAL' AND conversation_id IS NULL) OR "
            "(scope = 'CONVERSATION' AND conversation_id IS NOT NULL)",
            name="ck_kb_document_owner",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_kb_document_generation"),
    )
    op.create_index("ix_kb_document_scope", "kb_document", ["scope", "conversation_id"])
    op.create_index("ix_kb_document_status", "kb_document", ["status", "updated_at"])
    op.create_index(
        "uq_kb_document_content_scope",
        "kb_document",
        ["content_hash", "scope", "conversation_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "kb_ingest_outbox",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_document.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("generation >= 1 AND attempts >= 0", name="ck_kb_outbox_counts"),
    )
    op.create_index(
        "ix_kb_ingest_outbox_pending", "kb_ingest_outbox", ["published_at", "created_at"]
    )

    op.create_table(
        "kb_chunk",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_chunk.id", ondelete="CASCADE"),
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text()),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("level IN ('PARENT', 'CHILD')", name="ck_kb_chunk_level"),
        sa.CheckConstraint("chunk_index >= 0 AND token_count >= 0", name="ck_kb_chunk_counts"),
        sa.CheckConstraint(
            "(level = 'PARENT' AND parent_id IS NULL) OR "
            "(level = 'CHILD' AND parent_id IS NOT NULL)",
            name="ck_kb_chunk_parent",
        ),
        sa.UniqueConstraint("document_id", "level", "chunk_index", name="uq_kb_chunk_index"),
    )
    op.execute("ALTER TABLE kb_chunk ADD COLUMN embedding vector")
    op.create_index("ix_kb_chunk_document", "kb_chunk", ["document_id", "level"])
    op.create_index("ix_kb_chunk_parent", "kb_chunk", ["parent_id"])

    op.create_table(
        "annotation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text()),
        sa.Column("threshold", sa.Float(), server_default="0.92", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("scope IN ('GLOBAL', 'CONVERSATION')", name="ck_annotation_scope"),
        sa.CheckConstraint(
            "(scope = 'GLOBAL' AND conversation_id IS NULL) OR "
            "(scope = 'CONVERSATION' AND conversation_id IS NOT NULL)",
            name="ck_annotation_owner",
        ),
        sa.CheckConstraint(
            "threshold >= 0 AND threshold <= 1 AND hit_count >= 0",
            name="ck_annotation_threshold_hits",
        ),
    )
    op.execute("ALTER TABLE annotation ADD COLUMN embedding vector")
    op.create_index("ix_annotation_scope", "annotation", ["enabled", "scope", "conversation_id"])

    op.create_table(
        "annotation_match_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "annotation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation.id", ondelete="SET NULL"),
        ),
        sa.Column("query_hash", sa.Text(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("threshold", sa.Float()),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('HIT', 'MISS', 'ERROR')",
            name="ck_annotation_audit_outcome",
        ),
    )
    op.create_index(
        "ix_annotation_match_audit_created", "annotation_match_audit", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_annotation_match_audit_created", table_name="annotation_match_audit")
    op.drop_table("annotation_match_audit")
    op.drop_index("ix_annotation_scope", table_name="annotation")
    op.drop_table("annotation")
    op.drop_index("ix_kb_chunk_parent", table_name="kb_chunk")
    op.drop_index("ix_kb_chunk_document", table_name="kb_chunk")
    op.drop_table("kb_chunk")
    op.drop_index("ix_kb_ingest_outbox_pending", table_name="kb_ingest_outbox")
    op.drop_table("kb_ingest_outbox")
    op.drop_index("uq_kb_document_content_scope", table_name="kb_document")
    op.drop_index("ix_kb_document_status", table_name="kb_document")
    op.drop_index("ix_kb_document_scope", table_name="kb_document")
    op.drop_table("kb_document")
