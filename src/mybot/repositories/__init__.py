"""Persistence gateways over the conversation and message tables."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

conversations_table = sa.Table(
    "conversations",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("stable_key", sa.Text(), nullable=False, unique=True),
    sa.Column("connection_id", sa.Text(), nullable=False),
    sa.Column("platform", sa.Text(), nullable=False),
    sa.Column("chat_kind", sa.Text(), nullable=False),
    sa.Column("chat_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=True),
    sa.Column("ephemeral", sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
)

messages_table = sa.Table(
    "messages",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "conversation_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("direction", sa.Text(), nullable=False),
    sa.Column("platform_message_id", sa.Text(), nullable=True),
    sa.Column("sender_identity_id", sa.Text(), nullable=False),
    sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "ingested_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("trace_id", sa.Text(), nullable=True),
)

trace_spans_table = sa.Table(
    "trace_spans",
    metadata,
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
    sa.Column("duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
)

turns_table = sa.Table(
    "turns",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "conversation_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "inbound_message_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("action", sa.Text(), nullable=False),
    sa.Column("trigger", sa.Text(), nullable=False),
    sa.Column("model", sa.Text(), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
    sa.Column("outcome", sa.Text(), nullable=False),
    sa.Column("error", sa.Text(), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
)

tool_invocations_table = sa.Table(
    "tool_invocations",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "turn_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("tool_id", sa.Text(), nullable=False),
    sa.Column("ok", sa.Boolean(), nullable=False),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
)

llm_call_log_table = sa.Table(
    "llm_call_log",
    metadata,
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
    sa.Column("prompt_tokens", sa.Integer(), nullable=False),
    sa.Column("completion_tokens", sa.Integer(), nullable=False),
    sa.Column("latency_ms", sa.Integer(), nullable=False),
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
)

operator_audit_table = sa.Table(
    "operator_audit",
    metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("action", sa.Text(), nullable=False),
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
)

system_kv_table = sa.Table(
    "system_kv",
    metadata,
    sa.Column("key", sa.Text(), primary_key=True),
    sa.Column(
        "value",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
)

__all__ = [
    "conversations_table",
    "llm_call_log_table",
    "messages_table",
    "metadata",
    "operator_audit_table",
    "system_kv_table",
    "tool_invocations_table",
    "trace_spans_table",
    "turns_table",
]
