from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest

from .job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
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
            elif status in {"retryable_failed", "failed", "cancelled"}:
                connection.execute(
                    """
                    UPDATE gpu.jobs
                    SET status = 'pending',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        result = NULL,
                        last_error = NULL,
                        updated_at = now()
                    WHERE job_id = %s
                    """,
                    (request.job_id,),
                )
                row = {
                    **row,
                    "status": "pending",
                    "lease_expires_at": None,
                    "result": None,
                    "last_error": None,
                }

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
