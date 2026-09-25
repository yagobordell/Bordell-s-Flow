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


def validate_capacity_controller_dsn(dsn: str) -> str:
    """Reject known transaction-pooler DSNs that cannot hold session locks."""

    resolved = str(dsn or "").strip()
    if not resolved:
        raise RuntimeError("capacity controller Postgres DSN is required")

    from psycopg.conninfo import conninfo_to_dict

    try:
        info = conninfo_to_dict(resolved)
    except Exception as error:
        raise RuntimeError("capacity controller Postgres DSN is invalid") from error

    host = str(info.get("host") or "").strip().lower()
    port = str(info.get("port") or "").strip()
    if port == "6543" and (
        host.endswith(".supabase.co") or host.endswith(".supabase.com")
    ):
        raise RuntimeError(
            "Salad capacity controller requires a Supabase Direct or Session-mode "
            "Postgres connection; port 6543 is transaction pooling and cannot preserve "
            "the session advisory lock"
        )
    return resolved


def resolve_capacity_controller_dsn() -> str:
    """Resolve the dedicated controller DSN, falling back to the worker DSN."""

    return validate_capacity_controller_dsn(
        os.getenv("SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN")
        or os.getenv("POSTGRES_DSN")
        or ""
    )


@dataclass(frozen=True, slots=True)
class CapacityControllerHealth:
    healthy: bool
    controller_id: str | None
    status: str | None
    heartbeat_age_seconds: float | None
    reconcile_age_seconds: float | None
    last_error: str | None
    reason: str


class PostgresCapacityControllerOperationLock:
    """Session-scoped guard for manual capacity mutations.

    It uses the exact same advisory lock as controller leadership, so a live
    controller and an operator mutation can never own Salad capacity authority
    concurrently.
    """

    def __init__(self, dsn: str) -> None:
        import psycopg

        resolved = validate_capacity_controller_dsn(dsn)
        self._connection = psycopg.connect(resolved, autocommit=True)
        try:
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_KEY),
            ).fetchone()
            if row is None or not bool(row[0]):
                raise CapacityControllerLeadershipError(
                    "another Salad capacity controller or operator already owns "
                    "the Postgres advisory lock"
                )
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        if self._connection.closed:
            return
        try:
            self._connection.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_KEY),
            )
        finally:
            self._connection.close()

    def __enter__(self) -> PostgresCapacityControllerOperationLock:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


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

        dsn = validate_capacity_controller_dsn(dsn)
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
        if self._connection.closed:
            raise CapacityControllerLeadershipError(
                "capacity controller leadership connection is closed"
            )
        cursor = self._connection.execute(
            """
            UPDATE gpu.capacity_controller_state
            SET last_heartbeat_at = now(),
                updated_at = now()
            WHERE controller_name = %s
              AND controller_id = %s
            """,
            (self.controller_name, self.controller_id),
        )
        if cursor.rowcount != 1:
            raise CapacityControllerLeadershipError(
                "capacity controller lost ownership of its health record"
            )

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

    if status not in {"running", "degraded"}:
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
    else:
        reason = "healthy"
        if status == "degraded":
            reason = (
                "degraded but within reconciliation grace"
                + (f": {last_error}" if last_error else "")
            )
        return CapacityControllerHealth(
            healthy=True,
            controller_id=str(row["controller_id"]),
            status=status,
            heartbeat_age_seconds=heartbeat_age,
            reconcile_age_seconds=reconcile_age,
            last_error=last_error,
            reason=reason,
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
