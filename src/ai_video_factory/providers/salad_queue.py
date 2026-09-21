from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest

from .job_queue import (
    JobQueueClient,
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)

logger = logging.getLogger(__name__)


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
        request_sha256 = request.fingerprint()
        queue_metadata = {
            **dict(metadata),
            "application_job_id": request.job_id,
            "request_sha256": request_sha256,
        }
        try:
            payload = self._request(
                self._base_url,
                method="POST",
                body={
                    "input": request.model_dump(mode="json", exclude_none=True),
                    "metadata": queue_metadata,
                },
            )
        except TransientQueueError as exc:
            logger.warning(
                "Salad queue submit response was transiently unavailable; "
                "reconciling without resubmitting application_job_id=%s request_sha256=%s",
                request.job_id,
                request_sha256,
            )
            recovered = self._recover_ambiguous_submit(
                request=request,
                request_sha256=request_sha256,
            )
            if recovered is not None:
                logger.warning(
                    "Recovered ambiguous Salad queue submit application_job_id=%s "
                    "transport_job_id=%s status=%s",
                    request.job_id,
                    recovered.id,
                    recovered.status.value,
                )
                return recovered
            raise RuntimeError(
                "Salad queue submit outcome remained ambiguous after reconciliation for "
                f"application_job_id={request.job_id}; refusing unsafe duplicate POST"
            ) from exc
        return self._snapshot(payload)

    def _recover_ambiguous_submit(
        self,
        *,
        request: InferenceJobRequest,
        request_sha256: str,
        attempts: int = 6,
    ) -> QueueJobSnapshot | None:
        last_error: TransientQueueError | None = None
        for attempt in range(1, attempts + 1):
            try:
                matches = self._find_recoverable_jobs(
                    request=request,
                    request_sha256=request_sha256,
                )
                last_error = None
            except TransientQueueError as exc:
                matches = []
                last_error = exc

            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                rendered = ", ".join(
                    f"{item.id}:{item.status.value}" for item in matches
                )
                raise RuntimeError(
                    "Multiple recoverable Salad queue jobs match the same deterministic "
                    f"request {request.job_id}: {rendered}"
                )

            if attempt < attempts:
                delay_seconds = min(10.0, 2.0 * attempt)
                logger.warning(
                    "Salad submit reconciliation found no recoverable job yet "
                    "application_job_id=%s attempt=%s/%s delay_seconds=%.1f%s",
                    request.job_id,
                    attempt,
                    attempts,
                    delay_seconds,
                    (
                        f" last_error={last_error}"
                        if last_error is not None
                        else ""
                    ),
                )
                time.sleep(delay_seconds)

        return None

    def _find_recoverable_jobs(
        self,
        *,
        request: InferenceJobRequest,
        request_sha256: str,
    ) -> list[QueueJobSnapshot]:
        matches: list[QueueJobSnapshot] = []
        for page in range(1, 101):
            query = urllib.parse.urlencode({"page": page, "page_size": 100})
            payload = self._request(f"{self._base_url}?{query}")
            raw_items = payload.get("items", payload.get("jobs", []))
            if not isinstance(raw_items, list):
                raise RuntimeError("Salad queue list returned an invalid jobs payload")

            items = [item for item in raw_items if isinstance(item, dict)]
            for item in items:
                if not self._job_matches_request(
                    item,
                    request=request,
                    request_sha256=request_sha256,
                ):
                    continue
                snapshot = self._snapshot(item)
                if snapshot.status in {
                    QueueJobStatus.PENDING,
                    QueueJobStatus.RUNNING,
                    QueueJobStatus.SUCCEEDED,
                }:
                    matches.append(snapshot)

            if len(items) < 100:
                break
        return matches

    @staticmethod
    def _job_matches_request(
        item: Mapping[str, Any],
        *,
        request: InferenceJobRequest,
        request_sha256: str,
    ) -> bool:
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            if (
                metadata.get("application_job_id") == request.job_id
                and metadata.get("request_sha256") == request_sha256
            ):
                return True

        raw_input = item.get("input")
        if not isinstance(raw_input, Mapping):
            return False
        try:
            remote_request = InferenceJobRequest.model_validate(raw_input)
        except ValueError:
            return False
        return (
            remote_request.job_id == request.job_id
            and remote_request.fingerprint() == request_sha256
        )

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
            if method == "GET" and exc.code == 404:
                raise QueueJobNotFoundError(message) from exc
            if exc.code in {408, 429} or 500 <= exc.code < 600:
                raise TransientQueueError(message) from exc
            raise RuntimeError(message) from exc
        except TimeoutError as exc:
            message = f"Salad API {method} request timed out: {exc}"
            raise TransientQueueError(message) from exc
        except urllib.error.URLError as exc:
            message = f"Salad API {method} request failed: {exc.reason}"
            raise TransientQueueError(message) from exc

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
            provider_payload=dict(payload),
        )
