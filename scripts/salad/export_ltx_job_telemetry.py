"""Export durable LTX measurements and advisory per-instance comparisons.

Loads existing app/salad env files. Read-only query, no GPU, provider or R2 calls.
Apply infra/sql/009_ltx_job_telemetry.sql before running after deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ai_video_factory.inference.ltx_telemetry_analysis import compare_ltx_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Normalized measurements in JSONL")
    parser.add_argument("--report", type=Path, help="Advisory comparison in JSON")
    parser.add_argument("--max-rows", type=int, default=20000)
    args = parser.parse_args()
    if args.max_rows < 1 or args.max_rows > 100000:
        parser.error("--max-rows must be between 1 and 100000")

    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[2]
    for name in ("config/app.env", "config/salad.env", ".env"):
        load_dotenv(repo_root / name, override=False)
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        parser.error("POSTGRES_DSN is required; credentials must not be on the CLI")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, attempt_count, instance_id, stage, profile,
                       width, height, num_frames, inference_seconds,
                       task_seconds, pipeline_load_seconds, stage1_seconds,
                       stage2_seconds, video_decode_seconds, encode_seconds,
                       pipeline_reused, recorded_at
                FROM gpu.ltx_job_telemetry
                WHERE instance_id IS NOT NULL
                  AND inference_seconds IS NOT NULL
                  AND inference_seconds > 0
                ORDER BY recorded_at DESC, job_id DESC
                LIMIT %s
                """,
                (args.max_rows,),
            )
            rows = cursor.fetchall()
    documents = [
        {**row, "recorded_at": row["recorded_at"].isoformat()}
        for row in rows
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as target:
        for row in documents:
            target.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {
        "scope": "observed persisted LTX attempts; no claim of billable cost",
        "rows": len(documents),
        "instances": compare_ltx_telemetry(documents),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not documents:
        print("No persisted telemetry available yet", file=sys.stderr)


if __name__ == "__main__":
    main()
