from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

MIGRATION_SCRIPT = Path("scripts/salad/apply_ltx_telemetry_migration.py")


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("telemetry_migration", MIGRATION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_check_is_read_only_by_default() -> None:
    text = MIGRATION_SCRIPT.read_text(encoding="utf-8")
    assert 'parser.add_argument("--apply", action="store_true"' in text
    assert "if args.apply:" in text
    assert "connection.execute(sql, prepare=False)" in text
    assert "POSTGRES_DSN" in text
    assert 'parser.add_argument("--dsn"' not in text


def test_migration_schema_contract_matches_sql() -> None:
    module = _module()
    sql = Path("infra/sql/009_ltx_job_telemetry.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS gpu.ltx_job_telemetry" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "REFERENCES gpu.jobs" not in sql
    for key in module.EXPECTED_COLUMNS:
        assert key in sql
    module.validate_columns(dict(module.EXPECTED_COLUMNS))


def test_migration_rejects_incomplete_existing_table() -> None:
    module = _module()
    with pytest.raises(ValueError, match="missing/incompatible columns"):
        module.validate_columns({"job_id": "text"})
    with pytest.raises(ValueError, match="missing/incompatible columns"):
        module.validate_columns({**module.EXPECTED_COLUMNS, "job_id": "integer"})


class _Connection:
    def __init__(self, exists: bool, primary: bool = True) -> None:
        self.exists = exists
        self.primary = primary
        self.queries: list[str] = []

    def execute(self, sql: str) -> _Connection:
        self.queries.append(sql)
        return self

    def fetchone(self) -> tuple[object]:
        sql = self.queries[-1]
        if "to_regclass" in sql:
            return ("gpu.ltx_job_telemetry" if self.exists else None,)
        return (self.primary,)

    def fetchall(self) -> list[tuple[str, str]]:
        return list(_module().EXPECTED_COLUMNS.items())


def test_inspect_schema_handles_missing_ready_and_invalid_key() -> None:
    module = _module()
    assert module.inspect_schema(_Connection(False)) == "missing"
    assert module.inspect_schema(_Connection(True)) == "ready"
    with pytest.raises(ValueError, match="primary key"):
        module.inspect_schema(_Connection(True, primary=False))
