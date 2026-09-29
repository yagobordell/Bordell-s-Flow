"""Download per-attempt A2V GPU diagnostics from R2 without Salad log filters.

Example:
    uv run --locked --extra dev python scripts/salad/inspect_ltx_a2v_diagnostics.py \\
        --job-id ltx-a2v-b2-1A-1A-XXXXXXXXXXXX

Reads the operator's .env, queries the canonical Postgres job and downloads all
immutable diagnostic checkpoints (including full exception tracebacks, if emitted).
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import boto3
import psycopg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
EVENT_NAME_PATTERN = re.compile(r"^attempt-[0-9]{2}/[0-9]{3}-[a-z0-9_-]+\.json$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download LTX A2V GPU diagnostics from R2")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not JOB_ID_PATTERN.fullmatch(args.job_id):
        parser.error("--job-id must be a valid immutable inference job ID")
    load_dotenv(REPO_ROOT / ".env", override=False)

    output = args.output_dir or (
        REPO_ROOT / "data" / "output" / "ltx-a2v-diagnostics" / args.job_id
    )
    output.mkdir(parents=True, exist_ok=True)

    postgres_dsn = os.getenv("POSTGRES_DSN")
    if postgres_dsn:
        try:
            with psycopg.connect(postgres_dsn) as conn:
                row = conn.execute(
                    "SELECT job_id, status, attempt_count, last_error "
                    "FROM gpu.jobs WHERE job_id = %s",
                    (args.job_id,),
                ).fetchone()
        except psycopg.Error as error:
            print(
                f"Postgres unavailable ({type(error).__name__}); "
                "continuing with the independently stored R2 checkpoints."
            )
        else:
            if row is None:
                print(f"Postgres job not found: {args.job_id}")
            else:
                print(f"Postgres job={row[0]} status={row[1]} attempts={row[2]}")
                if row[3]:
                    print(f"Postgres last_error={row[3]}")
    else:
        print("POSTGRES_DSN not configured; inspecting R2 diagnostics only")

    names = (
        "R2_ENDPOINT_URL",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    )
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        parser.error("Missing R2 configuration: " + ", ".join(missing))

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    prefix = f"diagnostics/ltx-a2v/{args.job_id}/"
    saved = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for item in page.get("Contents", []):
            key = item["Key"]
            suffix = key.removeprefix(prefix)
            if not EVENT_NAME_PATTERN.fullmatch(suffix):
                continue
            target = output / suffix
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(target))
            print(f"R2 diagnostic saved: {target}")
            saved += 1

    if saved == 0:
        print(
            "No R2 checkpoints found. Check the job ID and Postgres last_error; "
            "the worker may have stopped before its first upload or R2 may have failed."
        )
        return 2
    print(f"Downloaded {saved} diagnostics into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
