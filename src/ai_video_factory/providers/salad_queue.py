from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest

from .job_queue import (
    JobQueueClient,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)


class SaladJobQueueClient(JobQueueClient):
    """Minimal Salad Job Queue HTTP adapter for model-specific inference workers."""

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
        request: InferenceJobRequest,
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

    def cancel(self, transport_job_id: str) -> None:
        self._request(
            f"{self._base_url}/{transport_job_id}",
            method="DELETE",
            expect_json=False,
        )

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        expect_json: bool = True,
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
                "User-Agent": "ai-video-factory-inference/1.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"Salad API returned HTTP {exc.code}: {detail}"
            if method == "GET" and (exc.code == 429 or 500 <= exc.code < 600):
                raise TransientQueueError(message) from exc
            raise RuntimeError(message) from exc
        except TimeoutError as exc:
            message = f"Salad API {method} request timed out: {exc}"
            if method == "GET":
                raise TransientQueueError(message) from exc
            raise RuntimeError(message) from exc
        except urllib.error.URLError as exc:
            message = f"Salad API request failed: {exc.reason}"
            if method == "GET":
                raise TransientQueueError(message) from exc
            raise RuntimeError(message) from exc

        if not expect_json:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Salad API returned invalid JSON") from exc
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
