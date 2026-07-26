"""Create the scoped, revocable memory store with pgvector embeddings."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260726_0005"
down_revision: str | None = "20260726_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("subject_identity_id", sa.Text(), nullable=True),
        sa.Column("conversation_stable_key", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("privacy", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "conflicts_with",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "supersedes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope IN ('GLOBAL', 'SUBJECT', 'CONVERSATION')",
            name=op.f("ck_memory_items_scope"),
        ),
        sa.CheckConstraint(
            "privacy IN ('PRIVATE', 'SHARED', 'PUBLIC', 'SENSITIVE')",
            name=op.f("ck_memory_items_privacy"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    # The embedding column is dimensionless pgvector so any embedding model
    # works; rows are tagged with embedding_model and queries filter on it,
    # keeping cosine comparisons within one model's dimensions.
    op.execute("ALTER TABLE memory_items ADD COLUMN embedding vector")
    op.create_index("ix_memory_items_subject", "memory_items", ["subject_identity_id"])
    op.create_index(
        "ix_memory_items_conversation", "memory_items", ["conversation_stable_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_conversation", table_name="memory_items")
    op.drop_index("ix_memory_items_subject", table_name="memory_items")
    op.drop_table("memory_items")
