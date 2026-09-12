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
    UnsupportedTaskError,
)
from .worker import InferenceWorker

logger = logging.getLogger(__name__)


def create_app(
    worker: InferenceWorker,
    *,
    prepare_in_background: bool = False,
    prepare_retry_seconds: float = 5.0,
) -> FastAPI:
    preparation_complete = threading.Event()
    preparation_stop = threading.Event()
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        prepare_thread: threading.Thread | None = None
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

        try:
            yield
        finally:
            preparation_stop.set()
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
        except (InputIntegrityError, UnsupportedTaskError) as exc:
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
