from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from ai_video_factory.inference.capacity_controller import (
    CapacityControllerLeadershipError,
    PostgresCapacityControllerLeadership,
    PostgresCapacityControllerOperationLock,
)


def _postgres_dsn() -> str:
    dsn = os.getenv("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    return dsn


def test_controller_leadership_blocks_manual_capacity_operation() -> None:
    dsn = _postgres_dsn()
    leadership = PostgresCapacityControllerLeadership(
        dsn,
        controller_id="test-controller-leader",
    )
    try:
        with pytest.raises(
            CapacityControllerLeadershipError,
            match="already owns the Postgres advisory lock",
        ):
            PostgresCapacityControllerOperationLock(dsn)
    finally:
        leadership.close()


def test_manual_capacity_operation_blocks_controller_startup() -> None:
    dsn = _postgres_dsn()
    with PostgresCapacityControllerOperationLock(dsn):
        with pytest.raises(
            CapacityControllerLeadershipError,
            match="already owns the Postgres advisory lock",
        ):
            PostgresCapacityControllerLeadership(
                dsn,
                controller_id="test-controller-blocked-by-operator",
            )


def test_operation_lock_detects_terminated_postgres_session() -> None:
    dsn = _postgres_dsn()
    lock = PostgresCapacityControllerOperationLock(dsn)
    backend_pid = lock.backend_pid
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            terminated = connection.execute(
                "SELECT pg_terminate_backend(%s)",
                (backend_pid,),
            ).fetchone()
        assert terminated is not None and bool(terminated[0])

        with pytest.raises(
            CapacityControllerLeadershipError,
            match="lost its PostgreSQL advisory-lock session",
        ):
            lock.assert_held()
    finally:
        lock.close()


def test_capacity_lock_helper_exits_when_postgres_session_is_lost() -> None:
    dsn = _postgres_dsn()
    environment = os.environ.copy()
    environment["SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN"] = dsn
    helper = subprocess.Popen(
        [
            sys.executable,
            "scripts/salad/hold_capacity_controller_lock.py",
            "--poll-seconds",
            "0.05",
        ],
        cwd=Path.cwd(),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline().strip() == "LOCK_ACQUIRED"

        with psycopg.connect(dsn, autocommit=True) as connection:
            row = connection.execute(
                """
                SELECT pid
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid = %s::oid
                  AND objid = %s::oid
                  AND objsubid = 2
                  AND granted
                """,
                (1_497_450_319, 1),
            ).fetchone()
            assert row is not None
            terminated = connection.execute(
                "SELECT pg_terminate_backend(%s)",
                (int(row[0]),),
            ).fetchone()
            assert terminated is not None and bool(terminated[0])

        assert helper.wait(timeout=5) == 4
        assert helper.stderr is not None
        assert "LOCK_LOST" in helper.stderr.read()
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
