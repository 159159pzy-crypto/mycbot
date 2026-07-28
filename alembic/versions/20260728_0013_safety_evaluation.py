"""Add moderation, approvals, evaluations, and message feedback.

Revision ID: 20260728_0013
Revises: 20260728_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_0013"
down_revision: str | None = "20260728_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moderation_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("point", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("flagged", sa.Boolean(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("preset_response", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "matched_terms",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("content_preview", sa.Text(), nullable=False),
        sa.Column("conversation_stable_key", sa.Text()),
        sa.Column("message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("trace_id", sa.Text()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "point IN ('inbound', 'outbound')",
            name="ck_moderation_audit_point",
        ),
        sa.CheckConstraint(
            "action IN ('direct_output', 'overridden')",
            name="ck_moderation_audit_action",
        ),
    )
    op.create_index(
        "ix_moderation_audit_created",
        "moderation_audit",
        ["created_at"],
    )
    op.create_index(
        "ix_moderation_audit_conversation_created",
        "moderation_audit",
        ["conversation_stable_key", "created_at"],
    )

    op.create_table(
        "tool_approval_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool_id", sa.Text(), nullable=False),
        sa.Column("conversation_stable_key", sa.Text(), nullable=False),
        sa.Column("actor_identity_id", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decision_note", sa.Text()),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')",
            name="ck_tool_approval_request_status",
        ),
    )
    op.create_index(
        "ix_tool_approval_request_pending",
        "tool_approval_request",
        ["status", "expires_at"],
    )

    op.create_table(
        "evaluation_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dataset_path", sa.Text(), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True)),
        sa.Column("persona_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column("model_channel", sa.Text()),
        sa.Column("judge_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_evaluation_run_status",
        ),
    )
    op.create_index("ix_evaluation_run_created", "evaluation_run", ["created_at"])

    op.create_table(
        "evaluation_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluation_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("case_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response", sa.Text()),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assertions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("passed", sa.Boolean()),
        sa.Column("judge_score", sa.Numeric(5, 4)),
        sa.Column("judge_reason", sa.Text()),
        sa.Column("model_channel", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "case_id", name="uq_evaluation_result_run_case"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_evaluation_result_status",
        ),
    )
    op.create_index(
        "ix_evaluation_result_run_status",
        "evaluation_result",
        ["run_id", "status"],
    )

    op.create_table(
        "message_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rating IN ('POSITIVE', 'NEGATIVE')",
            name="ck_message_feedback_rating",
        ),
    )
    op.create_index(
        "ix_message_feedback_updated",
        "message_feedback",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_feedback_updated", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_evaluation_result_run_status", table_name="evaluation_result")
    op.drop_table("evaluation_result")
    op.drop_index("ix_evaluation_run_created", table_name="evaluation_run")
    op.drop_table("evaluation_run")
    op.drop_index("ix_tool_approval_request_pending", table_name="tool_approval_request")
    op.drop_table("tool_approval_request")
    op.drop_index(
        "ix_moderation_audit_conversation_created",
        table_name="moderation_audit",
    )
    op.drop_index("ix_moderation_audit_created", table_name="moderation_audit")
    op.drop_table("moderation_audit")
