from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, status

from .contracts import GPUJobRequest, GPUJobResponse
from .errors import (
    InputIntegrityError,
    JobBusyError,
    JobConflictError,
    JobExecutionError,
    LeaseLostError,
    UnsupportedTaskError,
)
from .worker import GPUWorker

logger = logging.getLogger(__name__)


def create_app(worker: GPUWorker) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        worker.close()

    app = FastAPI(title="AI Video Factory GPU Worker", version="1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        try:
            worker.ready()
        except Exception as exc:
            logger.exception("worker readiness check failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="worker dependencies are not ready",
            ) from exc
        return {"status": "ready"}

    @app.post("/jobs", response_model=GPUJobResponse)
    def process_job(
        request: GPUJobRequest,
        salad_job_id: str | None = Header(default=None, alias="Salad-Job-Id"),
    ) -> GPUJobResponse:
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
            logger.exception("retryable GPU job failure")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="job execution failed; inspect worker logs",
            ) from exc
        except Exception as exc:
            logger.exception("unexpected GPU worker failure")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="unexpected worker failure; inspect worker logs",
            ) from exc

    return app
