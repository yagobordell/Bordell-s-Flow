"""Read-only owner diagnostics for the shared Salad capacity advisory lock.

Never acquires/releases the leadership lock, terminates a session, or stops a
container. PostgreSQL may hide activity details for other users; unknown
owners require human coordination, not -AllowControllerOverride.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ai_video_factory.inference.capacity_controller import (
    _ADVISORY_LOCK_KEY,
    _ADVISORY_LOCK_NAMESPACE,
    CONTROLLER_NAME,
    resolve_capacity_controller_dsn,
)


def inspect(connection: Any) -> dict[str, Any]:
    from psycopg.rows import dict_row

    connection.row_factory = dict_row
    # Two-key advisory locks are database-scoped. Never use
    # pg_try_advisory_lock here: this inspection must not change ownership.
    holders = connection.execute(
        """
        SELECT l.pid, a.application_name, a.state,
               EXTRACT(EPOCH FROM (now() - a.backend_start))::bigint
                   AS session_age_seconds
        FROM pg_locks AS l
        LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid
        WHERE l.locktype = 'advisory'
          AND l.classid = %s::oid AND l.objid = %s::oid
          AND l.objsubid = 2 AND l.granted
          AND l.database = (
              SELECT oid FROM pg_database WHERE datname = current_database()
          )
        ORDER BY l.pid
        """,
        (_ADVISORY_LOCK_NAMESPACE, _ADVISORY_LOCK_KEY),
    ).fetchall()
    try:
        status = connection.execute(
            """
            SELECT controller_id, status,
                   EXTRACT(EPOCH FROM (now() - last_heartbeat_at))::bigint
                       AS heartbeat_age_seconds,
                   EXTRACT(EPOCH FROM (now() - last_reconcile_at))::bigint
                       AS reconcile_age_seconds
            FROM gpu.capacity_controller_state
            WHERE controller_name = %s
            """,
            (CONTROLLER_NAME,),
        ).fetchone()
        health: dict[str, Any] = dict(status) if status else {}
    except Exception:
        # Monitoring table may not exist or this operator may lack SELECT.
        # Lock ownership visibility remains useful without it.
        health = {"readable": False}
    return {
        "advisory_lock_held": bool(holders),
        "holders": [dict(row) for row in holders],
        "controller_health_record": health,
        "caution": (
            "Application name and PID may not identify a human. "
            "Confirm the active operator and jobs before any manual Stop; "
            "never override or terminate lock holders based on this report alone."
        ),
    }


def main() -> int:
    try:
        import psycopg

        dsn = resolve_capacity_controller_dsn()
        with psycopg.connect(dsn, connect_timeout=8, autocommit=True) as connection:
            report = inspect(connection)
        print(json.dumps(report, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        # Do not include DSNs or raw DB exception text; it could contain secrets.
        print(
            f"Capacity lock read-only inspection unavailable ({type(exc).__name__}); "
            "consult the controller operator and inspect Salad Status.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
