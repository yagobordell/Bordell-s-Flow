from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ai_video_factory.compositor.media import MediaProbe
from ai_video_factory.inference.contracts import InferenceJobResponse, OutputArtifact
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.job_queue import (
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)


def _probe(path: Path) -> MediaProbe:
    is_output = "upscaled_clips" in path.as_posix()
    return MediaProbe(
        codec_name="h264",
        width=2560 if is_output else 1280,
        height=1440 if is_output else 720,
        fps=24.0,
        duration_seconds=1.0,
        frame_count=24,
        audio_stream_count=0,
    )


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, StoredObject]] = {}

    def stat(self, key: str) -> StoredObject | None:
        item = self.objects.get(key)
        return None if item is None else item[1]

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> StoredObject:
        data = source.read_bytes()
        stored = StoredObject(
            key=key,
            content_type=content_type,
            size_bytes=len(data),
            etag="etag",
            metadata=dict(metadata),
        )
        self.objects[key] = (data, stored)
        return stored

    def download(self, key: str, destination: Path) -> StoredObject:
        data, stored = self.objects[key]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return stored

    def ping(self) -> None:
        return None


class NoSubmitQueue:
    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, *_args, **_kwargs):
        self.submissions += 1
        raise AssertionError("valid Real-ESRGAN cache must prevent queue submission")

    def get(self, _job_id: str):
        raise AssertionError("valid Real-ESRGAN cache must prevent queue polling")


class TransientThenSuccessQueue:
    def __init__(self, storage: FakeStorage) -> None:
        self.storage = storage
        self.request = None
        self.get_calls = 0
        self.cancel_calls = 0

    def submit(self, request, *, metadata):
        del metadata
        self.request = request
        return QueueJobSnapshot(id="transport-1", status=QueueJobStatus.PENDING)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        assert transport_job_id == "transport-1"
        assert self.request is not None
        self.get_calls += 1
        if self.get_calls == 1:
            raise TransientQueueError("temporary Salad control-plane reset")

        content = b"upscaled-video"
        digest = hashlib.sha256(content).hexdigest()
        stored = StoredObject(
            key=self.request.output.key,
            content_type="video/mp4",
            size_bytes=len(content),
            etag="etag",
            metadata={
                "job-id": self.request.job_id,
                "request-sha256": self.request.fingerprint(),
                "artifact-sha256": digest,
            },
        )
        self.storage.objects[self.request.output.key] = (content, stored)
        response = InferenceJobResponse(
            job_id=self.request.job_id,
            request_sha256=self.request.fingerprint(),
            output=OutputArtifact(
                key=self.request.output.key,
                content_type="video/mp4",
                size_bytes=len(content),
                sha256=digest,
                etag="etag",
            ),
            attempt_count=1,
            replayed=False,
        ).model_dump(mode="json")
        return QueueJobSnapshot(
            id=transport_job_id,
            status=QueueJobStatus.SUCCEEDED,
            output=response,
        )

    def cancel(self, _transport_job_id: str) -> None:
        self.cancel_calls += 1


class OutputCommittedBeforeTerminalFailureQueue:
    def __init__(self, storage: FakeStorage) -> None:
        self.storage = storage
        self.request = None
        self.get_calls = 0

    def submit(self, request, *, metadata):
        del metadata
        self.request = request
        return QueueJobSnapshot(id="transport-failed", status=QueueJobStatus.PENDING)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        assert transport_job_id == "transport-failed"
        assert self.request is not None
        self.get_calls += 1
        content = b"durable-output-before-transport-failure"
        digest = hashlib.sha256(content).hexdigest()
        self.storage.objects[self.request.output.key] = (
            content,
            StoredObject(
                key=self.request.output.key,
                content_type="video/mp4",
                size_bytes=len(content),
                etag="etag",
                metadata={
                    "job-id": self.request.job_id,
                    "request-sha256": self.request.fingerprint(),
                    "artifact-sha256": digest,
                },
            ),
        )
        return QueueJobSnapshot(
            id=transport_job_id,
            status=QueueJobStatus.FAILED,
            provider_payload={"error": "transport lost after output commit"},
        )

    def cancel(self, _transport_job_id: str) -> None:
        raise AssertionError("verified durable output must avoid cancellation")
