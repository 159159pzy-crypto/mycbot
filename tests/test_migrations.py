import runpy
from pathlib import Path


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))

    def drop_index(self, name: str, *, table_name: str | None = None) -> None:
        self.calls.append(("drop_index", name))

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
