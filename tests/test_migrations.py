import runpy
from pathlib import Path


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))

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
