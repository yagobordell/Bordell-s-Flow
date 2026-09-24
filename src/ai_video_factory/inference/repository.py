from __future__ import annotations

import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import InferenceJobRequest
from .errors import JobConflictError, LeaseLostError, NonRetryableTaskError
from .ports import ClaimDecision, JobClaim

_NON_RETRYABLE_PREFIX = "NonRetryableTaskError:"


def _raise_if_non_retryable(last_error: str | None) -> None:
    if not last_error or not last_error.startswith(_NON_RETRYABLE_PREFIX):
        return
    detail = last_error.removeprefix(_NON_RETRYABLE_PREFIX).strip()
    raise NonRetryableTaskError(detail or "deterministic task rejection")


@dataclass(slots=True)
class _MemoryJob:
    request: InferenceJobRequest
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
        request: InferenceJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        transport_job_id: str | None,
    ) -> JobClaim:
        del transport_job_id
        now = datetime.now(UTC)
        with self._lock:
            row = self._jobs.setdefault(
                request.job_id,
                _MemoryJob(request=request, request_sha256=request_sha256),
            )
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
            if row.status == "retryable_failed":
                _raise_if_non_retryable(row.last_error)
            if row.status == "failed":
                _raise_if_non_retryable(row.last_error)
                raise NonRetryableTaskError(
                    row.last_error or f"job {request.job_id} is terminally failed"
                )
            if row.status == "cancelled":
                raise NonRetryableTaskError(f"job {request.job_id} was cancelled")
            if (
                row.status == "running"
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
            ):
                return JobClaim(ClaimDecision.BUSY, row.attempt_count)

            max_attempts = request.effective_max_attempts
            if row.attempt_count >= max_attempts:
                detail = (
                    f"job {request.job_id} exhausted max_attempts={max_attempts}; "
                    "refusing to claim another inference attempt"
                )
                row.status = "failed"
                row.last_error = detail
                row.lease_owner = None
                row.lease_expires_at = None
                raise NonRetryableTaskError(detail)

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
        retryable: bool,
    ) -> None:
        with self._lock:
            row = self._jobs.get(job_id)
            if (
                row is not None
                and row.request_sha256 == request_sha256
                and row.status == "running"
                and row.lease_owner == owner
            ):
                row.status = "retryable_failed" if retryable else "failed"
                row.last_error = error[:4000]
                row.lease_owner = None
                row.lease_expires_at = None

    def next_pending_request(
        self,
        task_names: tuple[str, ...],
    ) -> InferenceJobRequest | None:
        now = datetime.now(UTC)
        with self._lock:
            for row in self._jobs.values():
                if row.request.task not in task_names:
                    continue
                if row.status in {"pending", "retryable_failed"}:
                    return row.request.model_copy(deep=True)
                if (
                    row.status == "running"
                    and row.lease_expires_at is not None
                    and row.lease_expires_at <= now
                ):
                    return row.request.model_copy(deep=True)
        return None

    def is_instance_draining(self, instance_id: str) -> bool:
        del instance_id
        return False

    def ping(self) -> None:
        return None

    def close(self) -> None:
        return None


class PostgresJobRepository:
    """Transactional job state and lease ownership in Supabase/Postgres.

    The physical table remains ``gpu.jobs`` for backwards compatibility with existing
    deployments. It stores provider-neutral inference envelopes and can be migrated later
    without changing the worker contract.
    """

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
        request: InferenceJobRequest,
        request_sha256: str,
        *,
        owner: str,
        lease_seconds: int,
        transport_job_id: str | None,
    ) -> JobClaim:
        from psycopg.types.json import Jsonb

        manifest = request.model_dump(mode="json", exclude_none=True)
        exhausted_detail: str | None = None
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
                SELECT request_sha256, status, attempt_count, lease_expires_at, result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (request.job_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
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
            if row["status"] == "retryable_failed":
                _raise_if_non_retryable(row["last_error"])
            if row["status"] == "failed":
                _raise_if_non_retryable(row["last_error"])
                raise NonRetryableTaskError(
                    row["last_error"] or f"job {request.job_id} is terminally failed"
                )
            if row["status"] == "cancelled":
                raise NonRetryableTaskError(f"job {request.job_id} was cancelled")
            now = datetime.now(UTC)
            if (
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                return JobClaim(ClaimDecision.BUSY, int(row["attempt_count"]))

            max_attempts = request.effective_max_attempts
            if int(row["attempt_count"]) >= max_attempts:
                exhausted_detail = (
                    f"job {request.job_id} exhausted max_attempts={max_attempts}; "
                    "refusing to claim another inference attempt"
                )
                connection.execute(
                    """
                    UPDATE gpu.jobs
                    SET status = 'failed',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_error = %s,
                        updated_at = now()
                    WHERE job_id = %s
                      AND request_sha256 = %s
                    """,
                    (exhausted_detail[:4000], request.job_id, request_sha256),
                )
            else:
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
                if updated is None:  # pragma: no cover - defensive database invariant
                    raise RuntimeError(f"job row disappeared while claiming: {request.job_id}")
                return JobClaim(ClaimDecision.START, int(updated["attempt_count"]))

        if exhausted_detail is not None:
            raise NonRetryableTaskError(exhausted_detail)
        raise RuntimeError(f"job claim ended without a decision: {request.job_id}")

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
        retryable: bool,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s
                  AND request_sha256 = %s
                  AND status = 'running'
                  AND lease_owner = %s
                """,
                (
                    "retryable_failed" if retryable else "failed",
                    error[:4000],
                    job_id,
                    request_sha256,
                    owner,
                ),
            )

    def next_pending_request(
        self,
        task_names: tuple[str, ...],
    ) -> InferenceJobRequest | None:
        if not task_names:
            return None
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT request
                FROM gpu.jobs
                WHERE task = ANY(%s)
                  AND (
                      status = 'pending'
                      OR status = 'retryable_failed'
                      OR (
                          status = 'running'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= now()
                      )
                  )
                  AND (
                      status <> 'retryable_failed'
                      OR last_error IS NULL
                      OR last_error NOT LIKE %s
                  )
                ORDER BY created_at ASC, job_id ASC
                LIMIT 1
                """,
                (list(task_names), f"{_NON_RETRYABLE_PREFIX}%"),
            ).fetchone()
        if row is None:
            return None
        return InferenceJobRequest.model_validate(row["request"])

    def is_instance_draining(self, instance_id: str) -> bool:
        resolved = str(instance_id or "").strip()
        if not resolved:
            return False
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM gpu.capacity_drains
                WHERE instance_id = %s
                  AND expires_at > now()
                LIMIT 1
                """,
                (resolved,),
            ).fetchone()
        return row is not None

    def ping(self) -> None:
        with self._pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def close(self) -> None:
        self._pool.close()
