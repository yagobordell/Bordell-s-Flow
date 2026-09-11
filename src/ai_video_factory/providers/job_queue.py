from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ai_video_factory.inference.contracts import InferenceJobRequest


class QueueJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class QueueJobSnapshot:
    id: str
    status: QueueJobStatus
    output: Any = None


class JobQueueClient(Protocol):
    """Provider-neutral transport for submitting and observing inference jobs."""

    def submit(
        self,
        request: InferenceJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> QueueJobSnapshot: ...

    def get(self, transport_job_id: str) -> QueueJobSnapshot: ...
