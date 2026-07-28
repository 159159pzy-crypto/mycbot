"""Add conversation profiles, persona history, willingness, and heartbeat audit.

Revision ID: 20260728_0010
Revises: 20260728_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_0010"
down_revision: str | None = "20260728_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("relationship_score", sa.Numeric(5, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_memory_items_relationship_score",
        "memory_items",
        "relationship_score IS NULL OR "
        "(relationship_score >= 0 AND relationship_score <= 100)",
    )

    op.create_table(
        "agent_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("model_tier", sa.Text(), server_default="default", nullable=False),
        sa.Column(
            "tool_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "memory_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(
                "jsonb_build_object("
                "'enabled', true, "
                "'retrieval_limit', 5, "
                "'expression_examples', 3, "
                "'relationship_enabled', true)"
            ),
            nullable=False,
        ),
        sa.Column(
            "willingness_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(
                "jsonb_build_object("
                "'enabled', false, "
                "'threshold', 0.78, "
                "'sensitivity', 1.0, "
                "'keywords', jsonb_build_array())"
            ),
            nullable=False,
        ),
        sa.Column("active_persona_version_id", postgresql.UUID(as_uuid=True)),
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
    )
    op.create_table(
        "persona_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("persona_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("profile_id", "version", name="uq_persona_versions_profile_version"),
    )
    op.create_index(
        "ix_persona_versions_profile_version",
        "persona_versions",
        ["profile_id", "version"],
    )
    op.create_foreign_key(
        "fk_agent_profiles_active_persona_version",
        "agent_profiles",
        "persona_versions",
        ["active_persona_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "conversation_profile_bindings",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.add_column(
        "llm_call_log",
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "llm_call_log",
        sa.Column(
            "persona_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("persona_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "reply_willingness_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("threshold", sa.Numeric(5, 4), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1 AND threshold >= 0 AND threshold <= 1",
            name="ck_reply_willingness_scores",
        ),
    )
    op.create_index(
        "ix_reply_willingness_audit_conversation_created",
        "reply_willingness_audit",
        ["conversation_id", "created_at"],
    )

    op.create_table(
        "proactive_generation_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "persona_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("persona_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('sent', 'suppressed', 'error')",
            name="ck_proactive_generation_audit_outcome",
        ),
    )
    op.create_index(
        "ix_proactive_generation_audit_conversation_created",
        "proactive_generation_audit",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_proactive_generation_audit_conversation_created",
        table_name="proactive_generation_audit",
    )
    op.drop_table("proactive_generation_audit")
    op.drop_index(
        "ix_reply_willingness_audit_conversation_created",
        table_name="reply_willingness_audit",
    )
    op.drop_table("reply_willingness_audit")
    op.drop_table("conversation_profile_bindings")
    op.drop_column("llm_call_log", "persona_version_id")
    op.drop_column("llm_call_log", "profile_id")
    op.drop_constraint(
        "fk_agent_profiles_active_persona_version",
        "agent_profiles",
        type_="foreignkey",
    )
    op.drop_index("ix_persona_versions_profile_version", table_name="persona_versions")
    op.drop_table("persona_versions")
    op.drop_table("agent_profiles")
    op.drop_constraint(
        "ck_memory_items_relationship_score",
        "memory_items",
        type_="check",
    )
    op.drop_column("memory_items", "relationship_score")
