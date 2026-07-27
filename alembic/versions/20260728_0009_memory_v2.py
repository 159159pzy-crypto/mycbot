"""Add temporal memory, hybrid recall audit, and bounded core blocks.

Revision ID: 20260728_0009
Revises: 20260727_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_0009"
down_revision: str | None = "20260727_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column("memory_items", sa.Column("invalid_at", sa.DateTime(timezone=True)))
    op.add_column(
        "memory_items",
        sa.Column(
            "invalidated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="SET NULL"),
        ),
    )
    op.create_check_constraint(
        "ck_memory_items_invalidator_requires_time",
        "memory_items",
        "invalidated_by IS NULL OR invalid_at IS NOT NULL",
    )
    op.execute(
        """
        UPDATE memory_items
        SET invalid_at = revoked_at, revoked_at = NULL, revoked_reason = NULL
        WHERE revoked_reason IN ('expired', 'decayed')
        """
    )
    op.execute(
        """
        UPDATE memory_items AS old
        SET invalid_at = successor.created_at,
            invalidated_by = successor.id
        FROM memory_items AS successor,
             LATERAL jsonb_array_elements_text(
                 COALESCE(successor.supersedes, '[]'::jsonb)
             ) AS old_id(value)
        WHERE old.id = old_id.value::uuid AND old.invalid_at IS NULL
        """
    )
    op.create_index(
        "ix_memory_items_active_scope",
        "memory_items",
        ["scope", "subject_identity_id", "conversation_stable_key", "privacy"],
        postgresql_where=sa.text("revoked_at IS NULL AND invalid_at IS NULL"),
    )
    op.create_index(
        "ix_memory_items_content_trgm",
        "memory_items",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )

    op.create_table(
        "memory_operation_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("previous_memory_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_stable_key", sa.Text(), nullable=True),
        sa.Column("subject_identity_id", sa.Text(), nullable=True),
        sa.Column(
            "detail",
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
        sa.CheckConstraint(
            "operation IN ('ADD', 'UPDATE', 'DELETE', 'NOOP', 'PROMOTE', "
            "'INSIGHT', 'CORE_APPEND', 'CORE_REPLACE')",
            name="ck_memory_operation_audit_operation",
        ),
    )
    op.create_index(
        "ix_memory_operation_audit_created",
        "memory_operation_audit",
        ["created_at"],
    )
    op.create_index(
        "ix_memory_operation_audit_memory_created",
        "memory_operation_audit",
        ["memory_id", "created_at"],
    )

    op.create_table(
        "memory_recall_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("query_hash", sa.Text(), nullable=False),
        sa.Column("subject_identity_id", sa.Text(), nullable=False),
        sa.Column("conversation_stable_key", sa.Text(), nullable=False),
        sa.Column(
            "vector_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "text_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "selected_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_memory_recall_audit_created", "memory_recall_audit", ["created_at"]
    )

    op.create_table(
        "core_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("subject_identity_id", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.CheckConstraint(
            "label IN ('persona', 'user_profile')", name="ck_core_blocks_label"
        ),
        sa.CheckConstraint(
            "(label = 'persona' AND subject_identity_id IS NULL) OR "
            "(label = 'user_profile' AND subject_identity_id IS NOT NULL)",
            name="ck_core_blocks_owner",
        ),
        sa.CheckConstraint(
            "token_budget BETWEEN 50 AND 20000 AND version >= 1",
            name="ck_core_blocks_budget_version",
        ),
    )
    op.create_index(
        "uq_core_blocks_persona",
        "core_blocks",
        ["label"],
        unique=True,
        postgresql_where=sa.text("label = 'persona'"),
    )
    op.create_index(
        "uq_core_blocks_user_profile",
        "core_blocks",
        ["label", "subject_identity_id"],
        unique=True,
        postgresql_where=sa.text("label = 'user_profile'"),
    )


def downgrade() -> None:
    op.drop_index("uq_core_blocks_user_profile", table_name="core_blocks")
    op.drop_index("uq_core_blocks_persona", table_name="core_blocks")
    op.drop_table("core_blocks")
    op.drop_index("ix_memory_recall_audit_created", table_name="memory_recall_audit")
    op.drop_table("memory_recall_audit")
    op.drop_index(
        "ix_memory_operation_audit_memory_created",
        table_name="memory_operation_audit",
    )
    op.drop_index("ix_memory_operation_audit_created", table_name="memory_operation_audit")
    op.drop_table("memory_operation_audit")
    op.drop_index("ix_memory_items_content_trgm", table_name="memory_items")
    op.drop_index("ix_memory_items_active_scope", table_name="memory_items")
    op.drop_constraint(
        "ck_memory_items_invalidator_requires_time",
        "memory_items",
        type_="check",
    )
    op.drop_column("memory_items", "invalidated_by")
    op.drop_column("memory_items", "invalid_at")
