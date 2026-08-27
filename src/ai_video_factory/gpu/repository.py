from __future__ import annotations

import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import GPUJobRequest
from .errors import JobConflictError, LeaseLostError
from .ports import ClaimDecision, JobClaim


@dataclass(slots=True)
class _MemoryJob:
    request_sha256: str
    status: str = "pending"
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    last_error: str | None = None


class InMemoryJobRepository:
    """Process-local repository for development; production uses Postgres."""

    def __init__(self) -> None:
        self._jobs: dict[str, _MemoryJob] = {}
        self._lock = threading.Lock()

    def claim(
        self,
        request: GPUJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        transport_job_id: str | None,
    ) -> JobClaim:
        del transport_job_id
        now = datetime.now(UTC)
        with self._lock:
            row = self._jobs.setdefault(request.job_id, _MemoryJob(request_sha256))
            if row.request_sha256 != request_sha256:
                raise JobConflictError(
                    f"job_id {request.job_id!r} is already bound to a different request"
                )
            if row.status == "succeeded":
                return JobClaim(
                    ClaimDecision.REPLAY,
                    row.attempt_count,
                    deepcopy(row.result),
                )
            if (
                row.status == "running"
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
            ):
                return JobClaim(ClaimDecision.BUSY, row.attempt_count)

            row.status = "running"
            row.attempt_count += 1
            row.lease_owner = owner
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.last_error = None
            return JobClaim(ClaimDecision.START, row.attempt_count)

    def renew_lease(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        with self._lock:
            row = self._jobs.get(job_id)
            if (
                row is None
                or row.request_sha256 != request_sha256
                or row.status != "running"
                or row.lease_owner != owner
            ):
                return False
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            return True

    def mark_succeeded(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        result: Mapping[str, Any],
    ) -> None:
        with self._lock:
            row = self._jobs.get(job_id)
            if (
                row is None
                or row.request_sha256 != request_sha256
                or row.status != "running"
                or row.lease_owner != owner
            ):
                raise LeaseLostError(f"cannot commit job without its lease: {job_id}")
            row.status = "succeeded"
            row.result = deepcopy(dict(result))
            row.lease_owner = None
            row.lease_expires_at = None

    def mark_failed(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        error: str,
    ) -> None:
        with self._lock:
            row = self._jobs.get(job_id)
            if (
                row is not None
                and row.request_sha256 == request_sha256
                and row.status == "running"
                and row.lease_owner == owner
            ):
                row.status = "retryable_failed"
                row.last_error = error[:4000]
                row.lease_owner = None
                row.lease_expires_at = None

    def ping(self) -> None:
        return None

    def close(self) -> None:
        return None


class PostgresJobRepository:
    """Transactional job state and lease ownership in Supabase/Postgres."""

    def __init__(self, dsn: str, *, max_connections: int = 4) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=max_connections,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )

    def claim(
        self,
        request: GPUJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        transport_job_id: str | None,
    ) -> JobClaim:
        from psycopg.types.json import Jsonb

        manifest = request.model_dump(mode="json", exclude_none=True)
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                INSERT INTO gpu.jobs (
                    job_id, request_sha256, task, request, output_key, transport_job_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    request.job_id,
                    request_sha256,
                    request.task,
                    Jsonb(manifest),
                    request.output.key,
                    transport_job_id,
                ),
            )
            row = connection.execute(
                """
                SELECT request_sha256, status, attempt_count, lease_expires_at, result
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (request.job_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - impossible after insert without DB corruption
                raise RuntimeError(f"job row was not created: {request.job_id}")
            if row["request_sha256"] != request_sha256:
                raise JobConflictError(
                    f"job_id {request.job_id!r} is already bound to a different request"
                )
            if row["status"] == "succeeded":
                return JobClaim(
                    ClaimDecision.REPLAY,
                    int(row["attempt_count"]),
                    row["result"],
                )
            now = datetime.now(UTC)
            if (
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                return JobClaim(ClaimDecision.BUSY, int(row["attempt_count"]))

            updated = connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    transport_job_id = COALESCE(%s, transport_job_id),
                    last_error = NULL,
                    updated_at = now()
                WHERE job_id = %s
                RETURNING attempt_count
                """,
                (owner, lease_seconds, transport_job_id, request.job_id),
            ).fetchone()
            assert updated is not None
            return JobClaim(ClaimDecision.START, int(updated["attempt_count"]))

    def renew_lease(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE gpu.jobs
                SET lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE job_id = %s
                  AND request_sha256 = %s
                  AND status = 'running'
                  AND lease_owner = %s
                """,
                (lease_seconds, job_id, request_sha256, owner),
            )
            return cursor.rowcount == 1

    def mark_succeeded(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        result: Mapping[str, Any],
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'succeeded',
                    result = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE job_id = %s
                  AND request_sha256 = %s
                  AND status = 'running'
                  AND lease_owner = %s
                """,
                (Jsonb(dict(result)), job_id, request_sha256, owner),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"cannot commit job without its lease: {job_id}")

    def mark_failed(
        self,
        job_id: str,
        request_sha256: str,
        *,
        owner: str,
        error: str,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'retryable_failed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s
                  AND request_sha256 = %s
                  AND status = 'running'
                  AND lease_owner = %s
                """,
                (error[:4000], job_id, request_sha256, owner),
            )

    def ping(self) -> None:
        with self._pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def close(self) -> None:
        self._pool.close()
