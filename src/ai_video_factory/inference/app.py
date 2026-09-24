from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, status

from .contracts import InferenceJobRequest, InferenceJobResponse
from .errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    ModelBootstrapPendingError,
    NonRetryableTaskError,
    UnsupportedTaskError,
)
from .gpu_failures import is_retryable_gpu_failure
from .worker import InferenceWorker

logger = logging.getLogger(__name__)


def create_app(
    worker: InferenceWorker,
    *,
    prepare_in_background: bool = False,
    prepare_retry_seconds: float = 5.0,
    poll_jobs_from_repository: bool = False,
    job_poll_seconds: float = 2.0,
) -> FastAPI:
    if prepare_retry_seconds <= 0:
        raise ValueError("prepare_retry_seconds must be positive")
    if job_poll_seconds <= 0:
        raise ValueError("job_poll_seconds must be positive")

    preparation_complete = threading.Event()
    preparation_stop = threading.Event()
    job_poll_stop = threading.Event()
    preparation_error: Exception | None = None

    def prepare_worker() -> None:
        nonlocal preparation_error

        while not preparation_stop.is_set():
            try:
                worker.prepare()
            except ModelBootstrapPendingError as exc:
                logger.info("inference runtime is waiting for model files: %s", exc)
                if preparation_stop.wait(prepare_retry_seconds):
                    return
                continue
            except Exception as exc:
                preparation_error = exc
                preparation_complete.set()
                logger.exception("inference runtime preparation failed")
                return

            preparation_complete.set()
            logger.info("inference runtime prepared")
            return

    def poll_repository_jobs() -> None:
        while not job_poll_stop.is_set():
            if not preparation_complete.is_set():
                job_poll_stop.wait(min(job_poll_seconds, 1.0))
                continue
            if preparation_error is not None:
                return

            try:
                request = worker.next_pending_request()
            except Exception:
                logger.exception("failed to read the canonical Postgres inference queue")
                job_poll_stop.wait(job_poll_seconds)
                continue

            if request is None:
                job_poll_stop.wait(job_poll_seconds)
                continue

            try:
                worker.process(request, transport_job_id=request.job_id)
            except JobBusyError:
                job_poll_stop.wait(job_poll_seconds)
            except NonRetryableTaskError:
                logger.warning(
                    "terminal inference rejection persisted job_id=%s",
                    request.job_id,
                    exc_info=True,
                )
                job_poll_stop.wait(job_poll_seconds)
            except JobExecutionError as exc:
                logger.exception("inference job failed job_id=%s", request.job_id)
                delay = (
                    worker.gpu_retry_cooldown_seconds
                    if is_retryable_gpu_failure(exc)
                    else job_poll_seconds
                )
                job_poll_stop.wait(delay)
            except (
                InputIntegrityError,
                JobConflictError,
                LeaseLostError,
                UnsupportedTaskError,
            ):
                logger.exception("inference job failed job_id=%s", request.job_id)
                job_poll_stop.wait(job_poll_seconds)
            except Exception:
                logger.exception("unexpected repository job failure job_id=%s", request.job_id)
                job_poll_stop.wait(job_poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        prepare_thread: threading.Thread | None = None
        poll_thread: threading.Thread | None = None
        logger.info("preparing inference runtime")
        if prepare_in_background:
            prepare_thread = threading.Thread(
                target=prepare_worker,
                name="inference-worker-prepare",
                daemon=True,
            )
            prepare_thread.start()
        else:
            worker.prepare()
            preparation_complete.set()
            logger.info("inference runtime prepared")

        if poll_jobs_from_repository:
            poll_thread = threading.Thread(
                target=poll_repository_jobs,
                name="inference-postgres-job-poller",
                daemon=True,
            )
            poll_thread.start()

        try:
            yield
        finally:
            job_poll_stop.set()
            preparation_stop.set()
            if poll_thread is not None:
                poll_thread.join(timeout=max(job_poll_seconds * 2, 1))
            if prepare_thread is not None:
                prepare_thread.join(timeout=1)
            worker.close()

    app = FastAPI(title="AI Video Factory Inference Worker", version="1.0", lifespan=lifespan)

    def ensure_prepared() -> None:
        if not preparation_complete.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="worker runtime preparation is still in progress",
            )
        if preparation_error is not None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="worker runtime preparation failed",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        ensure_prepared()
        try:
            worker.ready()
        except Exception as exc:
            logger.exception("worker readiness check failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="worker dependencies are not ready",
            ) from exc
        return {"status": "ready"}

    @app.post("/jobs", response_model=InferenceJobResponse)
    def process_job(
        request: InferenceJobRequest,
        salad_job_id: str | None = Header(default=None, alias="Salad-Job-Id"),
    ) -> InferenceJobResponse:
        ensure_prepared()
        try:
            return worker.process(request, transport_job_id=salad_job_id)
        except JobBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
                headers={"Retry-After": "15"},
            ) from exc
        except JobConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (InputIntegrityError, NonRetryableTaskError, UnsupportedTaskError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except LeaseLostError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
                headers={"Retry-After": "15"},
            ) from exc
        except JobExecutionError as exc:
            logger.exception("retryable inference job failure")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="job execution failed; inspect worker logs",
            ) from exc
        except Exception as exc:
            logger.exception("unexpected inference worker failure")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="unexpected worker failure; inspect worker logs",
            ) from exc

    return app
