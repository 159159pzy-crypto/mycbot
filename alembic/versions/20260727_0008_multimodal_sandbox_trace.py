"""Add sandbox isolation and persisted trace spans.

Revision ID: 20260727_0008
Revises: 20260727_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260727_0008"
down_revision: str | None = "20260727_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("ephemeral", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("messages", sa.Column("trace_id", sa.Text(), nullable=True))
    op.create_index("ix_messages_trace_id", "messages", ["trace_id"])
    op.create_table(
        "trace_spans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "attributes",
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
            "status IN ('ok', 'error', 'skipped')",
            name="ck_trace_spans_status",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_trace_spans_duration"),
    )
    op.create_index(
        "ix_trace_spans_trace_created", "trace_spans", ["trace_id", "created_at"]
    )
    op.create_index(
        "ix_trace_spans_conversation_created",
        "trace_spans",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trace_spans_conversation_created", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_created", table_name="trace_spans")
    op.drop_table("trace_spans")
    op.drop_index("ix_messages_trace_id", table_name="messages")
    op.drop_column("messages", "trace_id")
    op.drop_column("conversations", "ephemeral")
