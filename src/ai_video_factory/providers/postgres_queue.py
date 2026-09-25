from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest, InferenceJobResponse

from .job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueRecoveryNotApplicableError,
    QueueJobStatus,
)


class PostgresJobQueueClient(JobQueueClient):
    """Application queue backed by gpu.jobs, the canonical inference job state."""

    def __init__(self, *, dsn: str, max_connections: int = 4) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN must be non-empty")
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=max_connections,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )

    def submit(
        self,
        request: InferenceJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> QueueJobSnapshot:
        del metadata
        from psycopg.types.json import Jsonb

        request_sha256 = request.fingerprint()
        manifest = request.model_dump(mode="json", exclude_none=True)
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                INSERT INTO gpu.jobs (
                    job_id,
                    request_sha256,
                    task,
                    request,
                    output_key,
                    transport_job_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    request.job_id,
                    request_sha256,
                    request.task,
                    Jsonb(manifest),
                    request.output.key,
                    request.job_id,
                ),
            )
            row = connection.execute(
                """
                SELECT job_id, request_sha256, status, attempt_count, lease_expires_at,
                       result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (request.job_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError(f"job row was not created: {request.job_id}")
            if row["request_sha256"] != request_sha256:
                raise RuntimeError(
                    f"job_id {request.job_id!r} is already bound to a different request"
                )

            status = str(row["status"])
            lease_expires_at = row["lease_expires_at"]
            if (
                status == "running"
                and lease_expires_at is not None
                and lease_expires_at <= datetime.now(UTC)
            ):
                connection.execute(
                    """
                    UPDATE gpu.jobs
                    SET status = 'pending',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE job_id = %s
                    """,
                    (request.job_id,),
                )
                row = {**row, "status": "pending", "lease_expires_at": None}

            # Preserve application state on idempotent resubmission. Workers may reclaim
            # retryable_failed work; failed/cancelled are never reopened as inference attempts.
            # A separate recovery-only operation may promote failed/retryable_failed to succeeded
            # only after an already committed durable bundle has been independently verified.

            return self._snapshot(row)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT job_id, request_sha256, status, attempt_count, lease_expires_at,
                       result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                """,
                (transport_job_id,),
            ).fetchone()
        if row is None:
            raise QueueJobNotFoundError(
                f"Postgres inference job does not exist: {transport_job_id}"
            )
        return self._snapshot(row)

    def reconcile_recovered_success(
        self,
        request: InferenceJobRequest,
        response: InferenceJobResponse,
    ) -> QueueJobSnapshot:
        """Commit a verified staged bundle without consuming another inference attempt."""

        from psycopg.types.json import Jsonb

        request_sha256 = request.fingerprint()
        if response.job_id != request.job_id or response.request_sha256 != request_sha256:
            raise RuntimeError("recovered inference response does not match the submitted request")

        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT job_id, request_sha256, status, attempt_count, lease_expires_at,
                       result, last_error
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (request.job_id,),
            ).fetchone()
            if row is None:
                raise QueueJobNotFoundError(
                    f"Postgres inference job does not exist: {request.job_id}"
                )
            if row["request_sha256"] != request_sha256:
                raise RuntimeError(
                    f"job_id {request.job_id!r} is already bound to a different request"
                )

            status = str(row["status"])
            if status == "succeeded":
                return self._snapshot(row)
            attempt_count = int(row["attempt_count"])
            recoverable = status in {"retryable_failed", "failed"} or (
                status == "pending" and attempt_count > 0
            )
            if not recoverable:
                raise QueueRecoveryNotApplicableError(
                    f"cannot reconcile recovered bundle while job {request.job_id} "
                    f"is in state {status!r}"
                )

            recovered = response.model_copy(
                update={
                    "attempt_count": attempt_count,
                    "replayed": True,
                }
            )
            updated = connection.execute(
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
                  AND (
                      status IN ('retryable_failed', 'failed')
                      OR (status = 'pending' AND attempt_count > 0)
                  )
                RETURNING job_id, request_sha256, status, attempt_count, lease_expires_at,
                          result, last_error
                """,
                (
                    Jsonb(recovered.model_dump(mode="json")),
                    request.job_id,
                    request_sha256,
                ),
            ).fetchone()
            if updated is None:  # pragma: no cover - row is locked above
                raise RuntimeError(
                    f"job {request.job_id} changed state during bundle recovery"
                )
            return self._snapshot(updated)

    def cancel(self, transport_job_id: str) -> None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT status, lease_expires_at
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (transport_job_id,),
            ).fetchone()
            if row is None:
                raise QueueJobNotFoundError(
                    f"Postgres inference job does not exist: {transport_job_id}"
                )

            status = str(row["status"])
            if status in {"succeeded", "failed", "cancelled"}:
                return
            if (
                status == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > datetime.now(UTC)
            ):
                raise RuntimeError(
                    f"cannot cancel actively leased inference job {transport_job_id}"
                )
            connection.execute(
                """
                UPDATE gpu.jobs
                SET status = 'cancelled',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE job_id = %s
                """,
                (transport_job_id,),
            )

    def close(self) -> None:
        self._pool.close()

    @staticmethod
    def _snapshot(row: Mapping[str, Any]) -> QueueJobSnapshot:
        raw_status = str(row["status"])
        if (
            raw_status == "running"
            and row.get("lease_expires_at") is not None
            and row["lease_expires_at"] <= datetime.now(UTC)
        ):
            status = QueueJobStatus.PENDING
        else:
            status = {
                "pending": QueueJobStatus.PENDING,
                "retryable_failed": QueueJobStatus.PENDING,
                "running": QueueJobStatus.RUNNING,
                "succeeded": QueueJobStatus.SUCCEEDED,
                "failed": QueueJobStatus.FAILED,
                "cancelled": QueueJobStatus.CANCELLED,
            }[raw_status]
        payload = {
            "job_id": str(row["job_id"]),
            "request_sha256": str(row["request_sha256"]),
            "status": raw_status,
            "attempt_count": int(row["attempt_count"]),
            "last_error": row.get("last_error"),
        }
        return QueueJobSnapshot(
            id=str(row["job_id"]),
            status=status,
            output=row.get("result"),
            provider_payload=payload,
        )
