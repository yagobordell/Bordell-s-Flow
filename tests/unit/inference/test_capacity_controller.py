from __future__ import annotations

from typing import Any

import psycopg
import pytest

from ai_video_factory.inference.capacity_controller import (
    CapacityControllerLeadershipError,
    PostgresCapacityControllerLeadership,
    read_capacity_controller_health,
    resolve_capacity_controller_dsn,
    validate_capacity_controller_dsn,
)


class _Result:
    def __init__(self, row: dict[str, Any] | None = None, *, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _LeadershipConnection:
    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state
        self.closed = False

    def execute(self, query: str, params: object = None) -> _Result:
        del params
        normalized = " ".join(query.split())
        if "pg_try_advisory_lock" in normalized:
            if self._state["locked"]:
                return _Result({"acquired": False})
            self._state["locked"] = True
            return _Result({"acquired": True})
        if "pg_advisory_unlock" in normalized:
            self._state["locked"] = False
            return _Result({"pg_advisory_unlock": True})
        return _Result(rowcount=1)

    def close(self) -> None:
        if not self.closed:
            self.closed = True


class _HealthConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def __enter__(self) -> _HealthConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> _Result:
        del query, params
        return _Result(self._row)


def test_only_one_capacity_controller_can_hold_leadership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"locked": False}
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *args, **kwargs: _LeadershipConnection(state),
    )

    leader = PostgresCapacityControllerLeadership(
        "postgresql://example",
        controller_id="leader-a",
    )
    with pytest.raises(CapacityControllerLeadershipError, match="already owns"):
        PostgresCapacityControllerLeadership(
            "postgresql://example",
            controller_id="leader-b",
        )

    leader.close()
    replacement = PostgresCapacityControllerLeadership(
        "postgresql://example",
        controller_id="leader-c",
    )
    replacement.close()


def test_recent_degraded_controller_is_healthy_within_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *args, **kwargs: _HealthConnection(
            {
                "controller_id": "leader-a",
                "status": "degraded",
                "heartbeat_age_seconds": 5.0,
                "reconcile_age_seconds": 20.0,
                "last_error": "RuntimeError: temporary Salad API timeout",
            }
        ),
    )

    health = read_capacity_controller_health(
        "postgresql://example",
        max_heartbeat_age_seconds=30,
        max_reconcile_age_seconds=60,
    )

    assert health.healthy is True
    assert "within reconciliation grace" in health.reason


def test_stale_reconciliation_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *args, **kwargs: _HealthConnection(
            {
                "controller_id": "leader-a",
                "status": "degraded",
                "heartbeat_age_seconds": 5.0,
                "reconcile_age_seconds": 180.0,
                "last_error": "RuntimeError: Salad unavailable",
            }
        ),
    )

    health = read_capacity_controller_health(
        "postgresql://example",
        max_heartbeat_age_seconds=30,
        max_reconcile_age_seconds=60,
    )

    assert health.healthy is False
    assert "reconciliation is stale" in health.reason


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres:secret@db.example.supabase.co:5432/postgres",
        "postgresql://postgres.example:secret@aws-0-eu.pooler.supabase.com:5432/postgres",
        "postgresql://postgres:secret@db.internal.example:6543/postgres",
    ],
)
def test_capacity_controller_accepts_session_capable_dsns(dsn: str) -> None:
    assert validate_capacity_controller_dsn(dsn) == dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres.example:secret@aws-0-eu.pooler.supabase.com:6543/postgres",
        "postgresql://postgres:secret@db.example.supabase.co:6543/postgres",
    ],
)
def test_capacity_controller_rejects_supabase_transaction_pooling(dsn: str) -> None:
    with pytest.raises(RuntimeError, match="port 6543"):
        validate_capacity_controller_dsn(dsn)


def test_dedicated_capacity_controller_dsn_overrides_worker_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "POSTGRES_DSN",
        "postgresql://postgres.example:secret@aws-0-eu.pooler.supabase.com:6543/postgres",
    )
    dedicated = (
        "postgresql://postgres.example:secret@aws-0-eu.pooler.supabase.com:5432/postgres"
    )
    monkeypatch.setenv("SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN", dedicated)

    assert resolve_capacity_controller_dsn() == dedicated
