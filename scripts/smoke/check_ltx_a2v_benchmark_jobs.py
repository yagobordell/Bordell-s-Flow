"""Fail-closed Postgres gate for controlled A2V Salad experiments.

Only jobs explicitly addressed to the experimental A2V task can run on its
GPU. This gate is deliberately read-only
and NEVER cancels an unrelated job. Use --terminal-failure-dir only after a
single submitted smoke job exits unsuccessfully; all A2V jobs must be terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]
_A2V_TASK = "video.ltx25.audio_to_video_experimental"
_ACTIVE_STATUSES = frozenset({"pending", "retryable_failed", "running"})


def find_unsettled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Include expired-running jobs: they can be reclaimed by any A2V worker."""
    return [row for row in rows if row["status"] in _ACTIVE_STATUSES]


def validate_terminal_failed_attempt(
    folder: Path, rows: list[dict[str, Any]], *, expected_segment: str
) -> str:
    """Allow continuing only after this attempt became terminal, never on timeout."""
    matches = list(folder.glob("job-request-*.json"))
    if len(matches) != 1:
        raise RuntimeError("Cannot identify exactly one submitted job for the failed profile")
    request = json.loads(matches[0].read_text(encoding="utf-8-sig"))
    job_id = request.get("job_id")
    # Input request is our own persisted job file, not a guessed prefix query.
    # The job-ID builder truncates long segment IDs before appending a hash.
    # Match the run/profile prefix, then use this exact persisted request ID
    # for the terminal-status query; never inspect or cancel another job.
    if (
        not isinstance(job_id, str)
        or not job_id.startswith("ltx-a2v-" + expected_segment[:32])
    ):
        raise RuntimeError("Failed attempt's job identity does not match the segment")
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal-failure-dir", type=Path)
    parser.add_argument("--expected-segment", default="")
    args = parser.parse_args()
    if bool(args.terminal_failure_dir) != bool(args.expected_segment):
        parser.error("--terminal-failure-dir and --expected-segment must be supplied together")
    for path in ("config/app.env", "config/salad.env", ".env"):
        load_dotenv(_ROOT / path, override=False)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise SystemExit("POSTGRES_DSN is required: refusing to allocate an experimental GPU")
    try:
        with psycopg.connect(dsn, connect_timeout=10) as connection:
            with connection.cursor() as cursor:
                # Never LIMIT a mixed terminal/active query: many failures
                # must not mask any runnable experimental job.
                cursor.execute(
                    """
                    SELECT job_id, status, lease_expires_at
                    FROM gpu.jobs WHERE task = %s
                      AND status IN ('pending', 'retryable_failed', 'running')
                    """,
                    (_A2V_TASK,),
                )
                rows = [
                    dict(zip(("job_id", "status", "lease_expires_at"), record, strict=True))
                    for record in cursor.fetchall()
                ]
                failed_job_id = (
                    validate_terminal_failed_attempt(
                        args.terminal_failure_dir, [], expected_segment=args.expected_segment
                    )
                    if args.terminal_failure_dir else None
                )
                failed_row = None
                if failed_job_id is not None:
                    cursor.execute(
                        """
                        SELECT status FROM gpu.jobs
                        WHERE task = %s AND job_id = %s
                        """,
                        (_A2V_TASK, failed_job_id),
                    )
                    failed_row = cursor.fetchone()
    except Exception as exc:
        print(f"A2V queue inspection failed; refusing to continue: {exc}", file=sys.stderr)
        return 1

    unsettled = find_unsettled(rows)
    if unsettled:
        for row in unsettled[:10]:
            print(
                f"A2V_QUEUE_BLOCKED job_id={row['job_id']} "
                f"status={row['status']} lease={row['lease_expires_at']}",
                file=sys.stderr,
            )
        return 1
    if args.terminal_failure_dir:
        if failed_row != ("failed",):
            print(
                f"Cannot continue: job {failed_job_id!r} is not terminal failed "
                "(possible lease, retry or media-validation mismatch)",
                file=sys.stderr,
            )
            return 1
        print(f"A2V_TERMINAL_FAILURE_SAFE job_id={failed_job_id}")
    print("A2V_QUEUE_IDLE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
