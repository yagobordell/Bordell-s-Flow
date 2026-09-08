from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ai_video_factory.gpu.contracts import GPUJobRequest

from .job_queue import JobQueueClient, QueueJobSnapshot, QueueJobStatus


class SaladJobQueueClient(JobQueueClient):
    """Minimal Salad Job Queue HTTP adapter used by the Phase 8 orchestrator."""

    def __init__(
        self,
        *,
        organization: str,
        project: str,
        queue_name: str,
        api_key: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._base_url = (
            "https://api.salad.com/api/public/organizations/"
            f"{organization}/projects/{project}/queues/{queue_name}/jobs"
        )

    def submit(
        self,
        request: GPUJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> QueueJobSnapshot:
        payload = self._request(
            self._base_url,
            method="POST",
            body={
                "input": request.model_dump(mode="json", exclude_none=True),
                "metadata": dict(metadata),
            },
        )
        return self._snapshot(payload)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        return self._snapshot(self._request(f"{self._base_url}/{transport_job_id}"))

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Salad-Api-Key": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ai-video-factory-phase8/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Salad API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Salad API request failed: {exc.reason}") from exc

        if not isinstance(value, dict):
            raise RuntimeError("Salad API returned a non-object JSON payload")
        return value

    @staticmethod
    def _snapshot(payload: dict[str, Any]) -> QueueJobSnapshot:
        try:
            transport_job_id = str(payload["id"])
            status = QueueJobStatus(str(payload["status"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"Unexpected Salad queue response: {payload}") from exc
        return QueueJobSnapshot(
            id=transport_job_id,
            status=status,
            output=payload.get("output"),
        )
