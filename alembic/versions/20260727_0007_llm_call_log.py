"""Add per-attempt model call accounting.

Revision ID: 20260727_0007
Revises: 20260726_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260727_0007"
down_revision: str | None = "20260726_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_call_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_price_per_million", sa.Numeric(18, 8), nullable=True),
        sa.Column("output_price_per_million", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('chat', 'memory', 'embedding', 'vision')",
            name="ck_llm_call_log_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('success', 'retryable_error', 'permanent_error')",
            name="ck_llm_call_log_status",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND latency_ms >= 0",
            name="ck_llm_call_log_nonnegative",
        ),
    )
    op.create_index(
        "ix_llm_call_log_channel_created",
        "llm_call_log",
        ["channel", "created_at"],
    )
    op.create_index(
        "ix_llm_call_log_conversation_created",
        "llm_call_log",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_log_conversation_created", table_name="llm_call_log")
    op.drop_index("ix_llm_call_log_channel_created", table_name="llm_call_log")
    op.drop_table("llm_call_log")
