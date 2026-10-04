"""Regression tests for read-only lock diagnostics and safe I2V cleanup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.salad.inspect_capacity_lock import inspect

ROOT = Path(__file__).resolve().parents[3]


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.row_factory = None

    def execute(self, sql: str, _params: tuple) -> SimpleNamespace:
        self.statements.append(sql)
        if "FROM pg_locks" in sql:
            return SimpleNamespace(
                fetchall=lambda: [
                    {
                        "pid": 1234,
                        "application_name": "capacity-controller",
                        "state": "idle",
                        "session_age_seconds": 250,
                    }
                ]
            )
        return SimpleNamespace(
            fetchone=lambda: {
                "controller_id": "operator-known",
                "status": "running",
                "heartbeat_age_seconds": 4,
                "reconcile_age_seconds": 10,
            }
        )


def test_inspector_never_attempts_lock_or_capacity_mutation() -> None:
    conn = FakeConnection()
    result = inspect(conn)
    assert result["advisory_lock_held"] is True
    assert result["holders"][0]["pid"] == 1234
    assert result["controller_health_record"]["controller_id"] == "operator-known"
    assert len(conn.statements) == 2
    assert all(statement.strip().upper().startswith("SELECT") for statement in conn.statements)
    joined = " ".join(conn.statements).lower()
    assert "pg_try_advisory_lock" not in joined
    assert "pg_terminate_backend" not in joined
    assert "pg_advisory_unlock" not in joined


def test_paid_wrapper_checks_both_digests_before_gpu_and_preserves_failures() -> None:
    ps1 = (
        ROOT / "scripts/smoke/run_ltx25_i2v_batch2_trial.ps1"
    ).read_text(encoding="utf-8")
    docker = ps1.index("verify_ltx25_i2v_hybrid_image.py")
    remote = ps1.index("verify_ltx25_i2v_remote_config.py")
    start = ps1.index("$StartRequested = $false")
    assert docker < remote < start
    assert "if ($CheckInputsOnly)" in ps1[:docker]
    assert "-AllowControllerOverride" not in ps1
    assert "inspect_capacity_lock.py" in ps1
    assert "$JobFailure = $_" in ps1
    assert "throw $JobFailure" in ps1
    assert "I2V CLEANUP UNVERIFIED" in ps1
    assert "Stop did not prove a stably stopped remote group" in ps1
    assert "$StartConfirmed = $true" in ps1
    assert ps1.index("$StartConfirmed = $true") < ps1.index("$Ready --service")
