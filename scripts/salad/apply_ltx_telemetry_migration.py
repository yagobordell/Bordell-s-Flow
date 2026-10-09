"""Check or apply the immutable LTX job telemetry migration.

Defaults to a read-only preflight. --apply executes only
infra/sql/009_ltx_job_telemetry.sql, then checks the resulting schema.

Credentials are loaded from existing local env files; never pass a DSN on CLI.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "infra" / "sql" / "009_ltx_job_telemetry.sql"

EXPECTED_COLUMNS = {
    "job_id": "text",
    "request_sha256": "character",
    "attempt_count": "integer",
    "recorded_at": "timestamp with time zone",
    "instance_id": "text",
    "instance_identity_source": "text",
    "worker_id": "text",
    "stage": "text",
    "profile": "text",
    "width": "integer",
    "height": "integer",
    "num_frames": "integer",
    "task_seconds": "double precision",
    "inference_seconds": "double precision",
    "pipeline_load_seconds": "double precision",
    "stage1_seconds": "double precision",
    "stage2_seconds": "double precision",
    "video_decode_seconds": "double precision",
    "encode_seconds": "double precision",
    "pipeline_reused": "boolean",
    "phase_seconds": "jsonb",
}


def validate_columns(actual: dict[str, str]) -> None:
    """Fail closed if an existing table does not match our write contract."""
    mismatches = {
        name: {"expected": expected, "actual": actual.get(name)}
        for name, expected in EXPECTED_COLUMNS.items()
        if actual.get(name) != expected
    }
    if mismatches:
        raise ValueError(
            "Existing gpu.ltx_job_telemetry has missing/incompatible columns: "
            + ", ".join(sorted(mismatches))
        )


def inspect_schema(connection: Any) -> str:
    row = connection.execute(
        "SELECT to_regclass('gpu.ltx_job_telemetry')"
    ).fetchone()
    if row is None or row[0] is None:
        return "missing"
    columns = connection.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'gpu' AND table_name = 'ltx_job_telemetry'
        """
    ).fetchall()
    validate_columns(dict(columns))
    primary = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint
            WHERE conrelid = 'gpu.ltx_job_telemetry'::regclass
              AND contype = 'p'
              AND conname = 'ltx_job_telemetry_pkey'
        )
        """
    ).fetchone()
    if not primary or not primary[0]:
        raise ValueError("gpu.ltx_job_telemetry is missing its expected primary key")
    return "ready"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply migration to Postgres")
    args = parser.parse_args()

    for file in ("config/app.env", "config/salad.env", ".env"):
        load_dotenv(REPO_ROOT / file, override=False)
    dsn = (os.environ.get("POSTGRES_DSN") or "").strip()
    if not dsn:
        parser.error("POSTGRES_DSN is missing; set it in the existing local .env")

    import psycopg

    # Autocommit avoids Psycopg wrapping the migration's explicit BEGIN/COMMIT
    # in another transaction. Never use a server-side prepared statement for
    # a multi-statement SQL migration.
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as connection:
        prior = inspect_schema(connection)
        if args.apply:
            sql = MIGRATION.read_text(encoding="utf-8")
            if "DROP " in sql.upper() or "TRUNCATE " in sql.upper():
                raise RuntimeError("Refusing a destructive telemetry migration")
            connection.execute(sql, prepare=False)
        after = inspect_schema(connection)

    print(json.dumps({
        "migration": MIGRATION.name,
        "prior": prior,
        "status": after,
        "applied": args.apply,
        "mode": "apply" if args.apply else "read_only_check",
    }, indent=2))
    if args.apply and after != "ready":
        raise SystemExit("Migration did not produce the required telemetry table")


if __name__ == "__main__":
    main()
