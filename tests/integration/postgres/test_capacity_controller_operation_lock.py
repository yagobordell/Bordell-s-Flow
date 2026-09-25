from __future__ import annotations

import os

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
