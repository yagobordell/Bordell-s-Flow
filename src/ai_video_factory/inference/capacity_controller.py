from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass

CONTROLLER_NAME = "salad-project"
_ADVISORY_LOCK_NAMESPACE = 1_497_450_319
_ADVISORY_LOCK_KEY = 1


class CapacityControllerLeadershipError(RuntimeError):
    """Another healthy process already owns Salad replica authority."""


@dataclass(frozen=True, slots=True)
class CapacityControllerHealth:
    healthy: bool
    controller_id: str | None
    status: str | None
    heartbeat_age_seconds: float | None
    reconcile_age_seconds: float | None
    last_error: str | None
    reason: str


class PostgresCapacityControllerLeadership:
    """Session-scoped Postgres leader election plus durable health reporting."""

    def __init__(
        self,
        dsn: str,
        *,
        controller_name: str = CONTROLLER_NAME,
        controller_id: str | None = None,
    ) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.controller_name = controller_name
        self.controller_id = controller_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        )
        self._connection = psycopg.connect(
            dsn,
            autocommit=True,
            row_factory=dict_row,
        )
        try:
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_KEY),
            ).fetchone()
            if row is None or not bool(row["acquired"]):
                raise CapacityControllerLeadershipError(
                    "another Salad capacity controller already owns the Postgres advisory lock"
                )
            self._connection.execute(
                """
                INSERT INTO gpu.capacity_controller_state (
                    controller_name,
                    controller_id,
                    status,
                    started_at,
                    last_heartbeat_at,
                    last_reconcile_at,
                    last_error,
                    updated_at
                ) VALUES (%s, %s, 'starting', now(), now(), NULL, NULL, now())
                ON CONFLICT (controller_name) DO UPDATE
                SET controller_id = EXCLUDED.controller_id,
                    status = 'starting',
                    started_at = now(),
                    last_heartbeat_at = now(),
                    last_reconcile_at = NULL,
                    last_error = NULL,
                    updated_at = now()
                """,
                (self.controller_name, self.controller_id),
            )
        except Exception:
            self._connection.close()
            raise

    def heartbeat(self) -> None:
        self._update_state(status="starting", last_error=None, reconciled=False)

    def record_success(self) -> None:
        self._update_state(status="running", last_error=None, reconciled=True)

    def record_failure(self, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"[:4000]
        self._update_state(status="degraded", last_error=detail, reconciled=False)

    def _update_state(
        self,
        *,
        status: str,
        last_error: str | None,
        reconciled: bool,
    ) -> None:
        if self._connection.closed:
            raise CapacityControllerLeadershipError(
                "capacity controller leadership connection is closed"
            )
        cursor = self._connection.execute(
            """
            UPDATE gpu.capacity_controller_state
            SET status = %s,
                last_heartbeat_at = now(),
                last_reconcile_at = CASE WHEN %s THEN now() ELSE last_reconcile_at END,
                last_error = %s,
                updated_at = now()
            WHERE controller_name = %s
              AND controller_id = %s
            """,
            (
                status,
                reconciled,
                last_error,
                self.controller_name,
                self.controller_id,
            ),
        )
        if cursor.rowcount != 1:
            raise CapacityControllerLeadershipError(
                "capacity controller lost ownership of its health record"
            )

    def close(self) -> None:
        if self._connection.closed:
            return
        try:
            self._connection.execute(
                """
                UPDATE gpu.capacity_controller_state
                SET status = 'stopped',
                    last_heartbeat_at = now(),
                    updated_at = now()
                WHERE controller_name = %s
                  AND controller_id = %s
                """,
                (self.controller_name, self.controller_id),
            )
            self._connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_KEY),
            )
        finally:
            self._connection.close()


def read_capacity_controller_health(
    dsn: str,
    *,
    controller_name: str = CONTROLLER_NAME,
    max_heartbeat_age_seconds: float = 120.0,
    max_reconcile_age_seconds: float = 120.0,
) -> CapacityControllerHealth:
    if max_heartbeat_age_seconds <= 0 or max_reconcile_age_seconds <= 0:
        raise ValueError("capacity controller health thresholds must be positive")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT
                controller_id,
                status,
                EXTRACT(EPOCH FROM (now() - last_heartbeat_at)) AS heartbeat_age_seconds,
                EXTRACT(EPOCH FROM (now() - last_reconcile_at)) AS reconcile_age_seconds,
                last_error
            FROM gpu.capacity_controller_state
            WHERE controller_name = %s
            """,
            (controller_name,),
        ).fetchone()

    if row is None:
        return CapacityControllerHealth(
            healthy=False,
            controller_id=None,
            status=None,
            heartbeat_age_seconds=None,
            reconcile_age_seconds=None,
            last_error=None,
            reason="capacity controller has never registered",
        )

    heartbeat_age = float(row["heartbeat_age_seconds"])
    reconcile_raw = row["reconcile_age_seconds"]
    reconcile_age = float(reconcile_raw) if reconcile_raw is not None else None
    status = str(row["status"])
    last_error = str(row["last_error"]) if row["last_error"] else None

    if status != "running":
        reason = f"capacity controller status is {status!r}"
    elif heartbeat_age > max_heartbeat_age_seconds:
        reason = (
            f"capacity controller heartbeat is stale ({heartbeat_age:.1f}s > "
            f"{max_heartbeat_age_seconds:.1f}s)"
        )
    elif reconcile_age is None:
        reason = "capacity controller has not completed a reconciliation"
    elif reconcile_age > max_reconcile_age_seconds:
        reason = (
            f"capacity controller reconciliation is stale ({reconcile_age:.1f}s > "
            f"{max_reconcile_age_seconds:.1f}s)"
        )
    elif last_error:
        reason = f"capacity controller reports an error: {last_error}"
    else:
        return CapacityControllerHealth(
            healthy=True,
            controller_id=str(row["controller_id"]),
            status=status,
            heartbeat_age_seconds=heartbeat_age,
            reconcile_age_seconds=reconcile_age,
            last_error=None,
            reason="healthy",
        )

    return CapacityControllerHealth(
        healthy=False,
        controller_id=str(row["controller_id"]),
        status=status,
        heartbeat_age_seconds=heartbeat_age,
        reconcile_age_seconds=reconcile_age,
        last_error=last_error,
        reason=reason,
    )
