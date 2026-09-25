from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ai_video_factory.inference.contracts import InferenceJobRequest, InferenceJobResponse


class QueueJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransientQueueError(RuntimeError):
    """Queue transport/API failure that is safe to retry while polling an existing job."""


class QueueJobNotFoundError(RuntimeError):
    """A previously persisted transport job no longer exists in the queue provider."""


@dataclass(frozen=True, slots=True)
class QueueJobSnapshot:
    id: str
    status: QueueJobStatus
    output: Any = None
    provider_payload: dict[str, Any] | None = None


class JobQueueClient(Protocol):
    """Provider-neutral transport for submitting and observing inference jobs."""

    def submit(
        self,
        request: InferenceJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> QueueJobSnapshot: ...

    def get(self, transport_job_id: str) -> QueueJobSnapshot: ...

    def cancel(self, transport_job_id: str) -> None: ...


@runtime_checkable
class RecoveryCapableJobQueueClient(Protocol):
    """Queue backends that can reconcile a verified durable bundle as success."""

    def reconcile_recovered_success(
        self,
        request: InferenceJobRequest,
        response: InferenceJobResponse,
    ) -> QueueJobSnapshot: ...
