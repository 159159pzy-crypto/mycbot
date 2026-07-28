"""Add platform DM pairing policies and requests.

Revision ID: 20260728_0012
Revises: 20260728_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260728_0012"
down_revision: str | None = "20260728_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pairing_policy",
        sa.Column("platform", sa.Text(), primary_key=True),
        sa.Column("connection_id", sa.Text(), primary_key=True),
        sa.Column("policy", sa.Text(), nullable=False, server_default="open"),
        sa.Column(
            "allowlist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "policy IN ('open', 'paired', 'allowlist')",
            name="ck_pairing_policy_mode",
        ),
    )
    op.create_table(
        "pairing_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("subject_identity_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("dismissed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_pairing_request_account_pending",
        "pairing_request",
        ["platform", "connection_id", "expires_at"],
    )
    op.create_table(
        "pairing_approval",
        sa.Column("platform", sa.Text(), primary_key=True),
        sa.Column("connection_id", sa.Text(), primary_key=True),
        sa.Column("subject_identity_id", sa.Text(), primary_key=True),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("pairing_approval")
    op.drop_index("ix_pairing_request_account_pending", table_name="pairing_request")
    op.drop_table("pairing_request")
    op.drop_table("pairing_policy")
