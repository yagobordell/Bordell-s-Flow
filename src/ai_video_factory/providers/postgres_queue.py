from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest, InferenceJobResponse
from ai_video_factory.inference.gpu_failures import is_fatal_gpu_context_failure

from .job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    QueueRecoveryNotApplicableError,
)


def _run_metadata(metadata: Mapping[str, str]) -> str | None:
    """Return application correlation metadata without coupling it to capacity."""

    run_id = str(metadata.get("run_id") or "").strip() or None
    # Rolling deployments may still send the retired field. Validate it only for
    # compatibility, but never persist or use it for scheduling new work.
    raw_count = str(metadata.get("run_job_count") or "").strip()
    if raw_count:
        run_job_count = int(raw_count)
        if run_job_count < 1:
            raise ValueError("run_job_count must be >= 1")
        if run_id is None:
            raise ValueError("run_id is required when legacy run_job_count is supplied")
    return run_id


def _legacy_run_metadata_fingerprint(
    request: InferenceJobRequest, *, run_id: str, run_job_count: int,
) -> str:
    parameters = dict(request.parameters)
    parameters.update(run_id=run_id, run_job_count=run_job_count)
    return request.model_copy(update={"parameters": parameters}).fingerprint()


def _legacy_run_metadata(row: Mapping[str, Any]) -> tuple[str | None, int | None]:
    legacy_run_id = str(row.get("run_id") or "").strip() or None
    legacy_count = row.get("run_job_count")
    request = row.get("request")
    if isinstance(request, Mapping):
        parameters = request.get("parameters")
        if isinstance(parameters, Mapping):
            legacy_run_id = legacy_run_id or (
                str(parameters.get("run_id") or "").strip() or None
            )
            if legacy_count is None:
                legacy_count = parameters.get("run_job_count")
    try:
        run_job_count = int(legacy_count) if legacy_count is not None else None
    except (TypeError, ValueError):
        run_job_count = None
    return legacy_run_id, run_job_count


def _is_fatal_gpu_context_error(error: object) -> bool:
    if not isinstance(error, str) or not error.strip():
        return False
    return is_fatal_gpu_context_failure(RuntimeError(error))


def _can_reopen_fatal_gpu_context_failure(
    row: Mapping[str, Any],
    *,
    request_sha256: str,
) -> bool:
    return (
        str(row.get("status") or "") in {"failed", "retryable_failed"}
        and str(row.get("request_sha256") or "") == request_sha256
        and _is_fatal_gpu_context_error(row.get("last_error"))
    )


def _can_rebind_legacy_run_metadata_failure(
    row: Mapping[str, Any],
    request: InferenceJobRequest,
    *,
    run_id: str | None,
) -> bool:
    if str(row.get("status")) != "failed":
        return False
    legacy_run_id, legacy_run_job_count = _legacy_run_metadata(row)
    if legacy_run_id is None or legacy_run_job_count is None:
        return False
    if run_id is not None and run_id != legacy_run_id:
        return False
    error = str(row.get("last_error") or "")
    if not (
        "ValidationError:" in error
        and "run_id" in error
        and "run_job_count" in error
        and ("extra_forbidden" in error or "Extra inputs are not permitted" in error)
    ):
        return False
    return str(row.get("request_sha256") or "") == _legacy_run_metadata_fingerprint(
        request,
        run_id=legacy_run_id,
        run_job_count=legacy_run_job_count,
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
        from psycopg.types.json import Jsonb

        run_id = _run_metadata(metadata)
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
                    transport_job_id,
                    run_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    request.job_id,
                    request_sha256,
                    request.task,
                    Jsonb(manifest),
                    request.output.key,
                    request.job_id,
                    run_id,
                ),
            )
            row = connection.execute(
                """
                SELECT job_id, request_sha256, request, status, attempt_count, lease_expires_at,
                       result, last_error, run_id, run_job_count, r2_pruned_at
                FROM gpu.jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (request.job_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError(f"job row was not created: {request.job_id}")
            if row["request_sha256"] != request_sha256:
                if not _can_rebind_legacy_run_metadata_failure(
                    row,
                    request,
                    run_id=run_id,
                ):
                    raise RuntimeError(
                        f"job_id {request.job_id!r} is already bound to a different request"
                    )
                legacy_run_id, legacy_run_job_count = _legacy_run_metadata(row)
                resolved_run_id = run_id or legacy_run_id
                connection.execute(
                    """
                    UPDATE gpu.jobs
                    SET request_sha256 = %s,
                        request = %s,
                        output_key = %s,
                        transport_job_id = %s,
                        run_id = %s,
                        run_job_count = %s,
                        status = 'pending',
                        attempt_count = 0,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        result = NULL,
                        last_error = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        updated_at = now()
                    WHERE job_id = %s
                      AND request_sha256 = %s
                      AND status = 'failed'
                    """,
                    (
                        request_sha256,
                        Jsonb(manifest),
                        request.output.key,
                        request.job_id,
                        resolved_run_id,
                        legacy_run_job_count,
                        request.job_id,
                        row["request_sha256"],
                    ),
                )
                row = {
                    **row,
                    "request_sha256": request_sha256,
                    "status": "pending",
                    "attempt_count": 0,
                    "lease_expires_at": None,
                    "result": None,
                    "last_error": None,
                    "run_id": resolved_run_id,
                    "run_job_count": legacy_run_job_count,
                }
            else:
                # R2 output bundles are disposable after a verified final MP4.
                # Preserve the succeeded row/timestamps for predictive autoscaling,
                # but explicitly reopen pruned requests if regeneration is needed.
                if row.get("r2_pruned_at") is not None:
                    connection.execute(
                        """
                        UPDATE gpu.jobs
                        SET status = 'pending',
                            attempt_count = 0,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            result = NULL,
                            last_error = NULL,
                            started_at = NULL,
                            finished_at = NULL,
                            r2_pruned_at = NULL,
                            updated_at = now()
                        WHERE job_id = %s
                          AND request_sha256 = %s
                          AND r2_pruned_at IS NOT NULL
                        """,
                        (request.job_id, request_sha256),
                    )
                    row = {
                        **row,
                        "status": "pending",
                        "attempt_count": 0,
                        "lease_expires_at": None,
                        "result": None,
                        "last_error": None,
                        "r2_pruned_at": None,
                    }
                existing_run_id = row.get("run_id")
                if run_id is not None and existing_run_id is None:
                    connection.execute(
                        """
                        UPDATE gpu.jobs
                        SET run_id = %s,
                            updated_at = now()
                        WHERE job_id = %s
                        """,
                        (run_id, request.job_id),
                    )
                    row = {**row, "run_id": run_id}
                    existing_run_id = run_id

                # run_id is correlation metadata only. An immutable request may be
                # referenced by multiple application runs; the canonical job identity
                # remains (job_id, request_sha256). Preserve active/succeeded state
                # instead of rejecting an identical request from another run.
                #
                # A cancelled job is different: cancellation is scoped to the run that
                # requested it. If another run needs the exact same immutable request,
                # reopen it with a fresh attempt budget and bind the execution to the
                # new run. Same-run resubmission remains cancelled/idempotent.
                if (
                    str(row["status"]) == "cancelled"
                    and run_id is not None
                    and existing_run_id not in (None, run_id)
                ):
                    connection.execute(
                        """
                        UPDATE gpu.jobs
                        SET status = 'pending',
                            attempt_count = 0,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            result = NULL,
                            last_error = NULL,
                            started_at = NULL,
                            finished_at = NULL,
                            run_id = %s,
                            updated_at = now()
                        WHERE job_id = %s
                          AND request_sha256 = %s
                          AND status = 'cancelled'
                        """,
                        (run_id, request.job_id, request_sha256),
                    )
                    row = {
                        **row,
                        "status": "pending",
                        "attempt_count": 0,
                        "lease_expires_at": None,
                        "result": None,
                        "last_error": None,
                        "run_id": run_id,
                    }

                if _can_reopen_fatal_gpu_context_failure(
                    row,
                    request_sha256=request_sha256,
                ):
                    connection.execute(
                        """
                        UPDATE gpu.jobs
                        SET status = 'pending',
                            attempt_count = 0,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            result = NULL,
                            last_error = NULL,
                            started_at = NULL,
                            finished_at = NULL,
                            updated_at = now()
                        WHERE job_id = %s
                          AND request_sha256 = %s
                          AND status IN ('failed', 'retryable_failed')
                        """,
                        (request.job_id, request_sha256),
                    )
                    row = {
                        **row,
                        "status": "pending",
                        "attempt_count": 0,
                        "lease_expires_at": None,
                        "result": None,
                        "last_error": None,
                    }

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
            lease_expires_at = row["lease_expires_at"]
            expired_running = (
                status == "running"
                and lease_expires_at is not None
                and lease_expires_at <= datetime.now(UTC)
            )
            recoverable = (
                status in {"retryable_failed", "failed"}
                or (status == "pending" and attempt_count > 0)
                or expired_running
            )
            if not recoverable:
                snapshot = self._snapshot(row)
                raise QueueRecoveryNotApplicableError(
                    f"cannot reconcile recovered bundle while job {request.job_id} "
                    f"is in state {status!r}",
                    status=snapshot.status,
                    raw_status=status,
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
                      OR (
                          status = 'running'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= now()
                      )
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

    @contextmanager
    def prune_scoped_video_run(
        self, scope: str, required_job_ids: Sequence[str],
    ) -> Iterator[list[str]]:
        """Lock and retire every terminal, isolated GPU request in a video run.

        All jobs are validated before deleting a single R2 object. In-flight
        jobs prevent pruning. The queue cache rows are removed only after R2
        deletion succeeds, so fresh requests never replay an evicted success.
        """
        if len(scope) != 24 or any(char not in "0123456789abcdef" for char in scope):
            raise ValueError("Invalid R2 run scope")
        required = set(required_job_ids)
        if not required:
            raise ValueError("No required LTX jobs for cleanup")
        suffix = f"-r{scope}"
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """
                SELECT job_id, status, request
                FROM gpu.jobs
                WHERE job_id LIKE %s
                ORDER BY job_id
                FOR UPDATE
                """,
                (f"%{suffix}",),
            ).fetchall()
            found = {str(row["job_id"]) for row in rows}
            if not required.issubset(found):
                raise RuntimeError("R2 cleanup requires all completed LTX queue rows")
            for row in rows:
                job_id = str(row["job_id"])
                request = row["request"]
                prefix = f"jobs/{job_id}/"
                if row["status"] not in {"succeeded", "failed", "cancelled"}:
                    raise RuntimeError("R2 cleanup deferred: GPU job is not terminal")
                if not isinstance(request, dict):
                    raise RuntimeError("R2 cleanup requires a valid immutable request")
                inputs = request.get("inputs", [])
                output = request.get("output")
                sidecars = request.get("sidecar_outputs", {})
                if (
                    not isinstance(inputs, list)
                    or not all(
                        isinstance(item, dict)
                        and (
                            (item.get("name") == "reference_audio"
                             and isinstance(item.get("key"), str)
                             and bool(item["key"]))
                            or str(item.get("key", "")).startswith(prefix + "inputs/")
                        )
                        for item in inputs
                    )
                    or not isinstance(output, dict)
                    or not str(output.get("key", "")).startswith(prefix)
                    or not isinstance(sidecars, dict)
                    or not all(
                        isinstance(item, dict)
                        and str(item.get("key", "")).startswith(prefix)
                        for item in sidecars.values()
                    )
                ):
                    raise RuntimeError(f"R2 cleanup refused an unscoped input: {job_id}")
            ids = sorted(found)
            yield ids
            result = connection.execute(
                """
                UPDATE gpu.jobs
                SET r2_pruned_at = now(),
                    updated_at = now()
                WHERE job_id = ANY(%s)
                  AND status IN ('succeeded', 'failed', 'cancelled')
                  AND r2_pruned_at IS NULL
                """,
                (ids,),
            )
            if result.rowcount != len(ids):
                raise RuntimeError("R2 cleanup cache-pruning marker was incomplete")

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
