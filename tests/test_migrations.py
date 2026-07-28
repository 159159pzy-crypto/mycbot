import runpy
from pathlib import Path


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))

    def drop_index(self, name: str, *, table_name: str | None = None) -> None:
        self.calls.append(("drop_index", name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.calls.append(("drop_column", f"{table_name}.{column_name}"))

    def drop_constraint(
        self, name: str, table_name: str, *, type_: str | None = None
    ) -> None:
        self.calls.append(("drop_constraint", f"{table_name}.{name}"))

    def execute(self, statement: str) -> None:
        self.calls.append(("execute", statement))


def test_foundation_downgrade_preserves_shared_vector_extension() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260714_0001_foundation.py"
    )
    namespace = runpy.run_path(str(migration_path))
    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [("drop_table", "system_kv")]


def test_messaging_spine_migration_chains_and_downgrade_drops_only_new_tables() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260726_0002_conversations_messages.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260726_0002"
    assert namespace["down_revision"] == "20260714_0001"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_messages_conversation_occurred"),
        ("drop_table", "messages"),
        ("drop_table", "conversations"),
    ]
    assert all("vector" not in str(call).lower() for call in operations.calls)
    assert all("system_kv" not in str(call) for call in operations.calls)


def test_turns_migration_chains_and_downgrade_drops_only_the_audit_table() -> None:
    migration_path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "20260726_0003_turns.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260726_0003"
    assert namespace["down_revision"] == "20260726_0002"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_turns_conversation_created"),
        ("drop_table", "turns"),
    ]


def test_tool_invocations_migration_chains_and_downgrade_drops_only_the_audit_table() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260726_0004_tool_invocations.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260726_0004"
    assert namespace["down_revision"] == "20260726_0003"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_tool_invocations_turn"),
        ("drop_table", "tool_invocations"),
    ]


def test_memory_migration_chains_and_downgrade_preserves_the_vector_extension() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260726_0005_memory_items.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260726_0005"
    assert namespace["down_revision"] == "20260726_0004"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_memory_items_conversation"),
        ("drop_index", "ix_memory_items_subject"),
        ("drop_table", "memory_items"),
    ]
    assert all("vector" not in str(call).lower() for call in operations.calls)


def test_operator_audit_migration_chains_and_downgrade_drops_only_its_table() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260726_0006_operator_audit.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260726_0006"
    assert namespace["down_revision"] == "20260726_0005"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_operator_audit_created"),
        ("drop_table", "operator_audit"),
    ]


def test_llm_call_log_migration_chains_and_downgrade_drops_only_its_table() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260727_0007_llm_call_log.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260727_0007"
    assert namespace["down_revision"] == "20260726_0006"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations

    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_llm_call_log_conversation_created"),
        ("drop_index", "ix_llm_call_log_channel_created"),
        ("drop_table", "llm_call_log"),
    ]


def test_multimodal_sandbox_trace_migration_chains_and_downgrades_cleanly() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260727_0008_multimodal_sandbox_trace.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260727_0008"
    assert namespace["down_revision"] == "20260727_0007"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations
    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_trace_spans_conversation_created"),
        ("drop_index", "ix_trace_spans_trace_created"),
        ("drop_table", "trace_spans"),
        ("drop_index", "ix_messages_trace_id"),
        ("drop_column", "messages.trace_id"),
        ("drop_column", "conversations.ephemeral"),
    ]


def test_memory_v2_migration_chains_and_preserves_shared_extensions() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260728_0009_memory_v2.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260728_0009"
    assert namespace["down_revision"] == "20260727_0008"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations
    downgrade()

    assert operations.calls == [
        ("drop_index", "uq_core_blocks_user_profile"),
        ("drop_index", "uq_core_blocks_persona"),
        ("drop_table", "core_blocks"),
        ("drop_index", "ix_memory_recall_audit_created"),
        ("drop_table", "memory_recall_audit"),
        ("drop_index", "ix_memory_operation_audit_memory_created"),
        ("drop_index", "ix_memory_operation_audit_created"),
        ("drop_table", "memory_operation_audit"),
        ("drop_index", "ix_memory_items_content_trgm"),
        ("drop_index", "ix_memory_items_active_scope"),
        (
            "drop_constraint",
            "memory_items.ck_memory_items_invalidator_requires_time",
        ),
        ("drop_column", "memory_items.invalidated_by"),
        ("drop_column", "memory_items.invalid_at"),
    ]
    assert all("DROP EXTENSION" not in call[1] for call in operations.calls)


def test_group_personality_migration_chains_and_downgrades_cleanly() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260728_0010_group_personality.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260728_0010"
    assert namespace["down_revision"] == "20260728_0009"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations
    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_proactive_generation_audit_conversation_created"),
        ("drop_table", "proactive_generation_audit"),
        ("drop_index", "ix_reply_willingness_audit_conversation_created"),
        ("drop_table", "reply_willingness_audit"),
        ("drop_table", "conversation_profile_bindings"),
        ("drop_column", "llm_call_log.persona_version_id"),
        ("drop_column", "llm_call_log.profile_id"),
        ("drop_constraint", "agent_profiles.fk_agent_profiles_active_persona_version"),
        ("drop_index", "ix_persona_versions_profile_version"),
        ("drop_table", "persona_versions"),
        ("drop_table", "agent_profiles"),
        ("drop_constraint", "memory_items.ck_memory_items_relationship_score"),
        ("drop_column", "memory_items.relationship_score"),
    ]


def test_knowledge_migration_chains_and_preserves_shared_vector_extension() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260728_0011_knowledge_base.py"
    )
    namespace = runpy.run_path(str(migration_path))
    assert namespace["revision"] == "20260728_0011"
    assert namespace["down_revision"] == "20260728_0010"

    downgrade = namespace["downgrade"]
    operations = RecordingOperations()
    downgrade.__globals__["op"] = operations
    downgrade()

    assert operations.calls == [
        ("drop_index", "ix_annotation_match_audit_created"),
        ("drop_table", "annotation_match_audit"),
        ("drop_index", "ix_annotation_scope"),
        ("drop_table", "annotation"),
        ("drop_index", "ix_kb_chunk_parent"),
        ("drop_index", "ix_kb_chunk_document"),
        ("drop_table", "kb_chunk"),
        ("drop_index", "ix_kb_ingest_outbox_pending"),
        ("drop_table", "kb_ingest_outbox"),
        ("drop_index", "uq_kb_document_content_scope"),
        ("drop_index", "ix_kb_document_status"),
        ("drop_index", "ix_kb_document_scope"),
        ("drop_table", "kb_document"),
    ]
    assert all("DROP EXTENSION" not in call[1] for call in operations.calls)
