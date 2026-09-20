from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read one persisted inference job failure from Postgres without mutating state."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--transport-job-id")
    group.add_argument("--application-job-id")
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(args.env_file, override=False)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("POSTGRES_DSN is missing.")

    import psycopg
    from psycopg.rows import dict_row

    if args.transport_job_id:
        predicate = "transport_job_id = %s"
        value = args.transport_job_id
    else:
        predicate = "job_id = %s"
        value = args.application_job_id

    query = f"""
        SELECT
            job_id,
            transport_job_id,
            status,
            attempt_count,
            lease_owner,
            lease_expires_at,
            request,
            last_error,
            updated_at
        FROM gpu.jobs
        WHERE {predicate}
        ORDER BY updated_at DESC
        LIMIT 1
    """

    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row) as connection:
        row = connection.execute(query, (value,)).fetchone()

    if row is None:
        raise SystemExit("No persisted gpu.jobs row matched the requested identifier.")

    print(f"application_job_id={row['job_id']}")
    print(f"transport_job_id={row['transport_job_id']}")
    print(f"status={row['status']}")
    print(f"attempt_count={row['attempt_count']}")
    print(f"lease_owner={row['lease_owner'] or '<none>'}")
    print(f"lease_expires_at={row['lease_expires_at'] or '<none>'}")
    request = row["request"] or {}
    parameters = request.get("parameters", {}) if isinstance(request, dict) else {}
    print(f"task={request.get('task', '<unknown>') if isinstance(request, dict) else '<unknown>'}")
    print(f"width={parameters.get('width', '<unknown>')}")
    print(f"height={parameters.get('height', '<unknown>')}")
    print(f"fps={parameters.get('fps', '<unknown>')}")
    print(f"num_frames={parameters.get('num_frames', '<unknown>')}")
    print(f"generation_profile={parameters.get('generation_profile', '<unknown>')}")
    print(f"updated_at={row['updated_at']}")
    print(f"last_error={row['last_error'] or '<none>'}")


if __name__ == "__main__":
    main()
