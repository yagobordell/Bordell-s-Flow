from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

from PIL import Image

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    OutputArtifact,
)
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.content_types: dict[str, str] = {}

    def download(self, key: str, destination: Path) -> StoredObject:
        content = self.objects[key]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return self._stored(key)

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject:
        self.objects[key] = source.read_bytes()
        self.metadata[key] = dict(metadata)
        self.content_types[key] = content_type
        return self._stored(key, content_type=content_type)

    def stat(self, key: str) -> StoredObject | None:
        if key not in self.objects:
            return None
        return self._stored(key)

    def ping(self) -> None:
        return None

    def _stored(
        self,
        key: str,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        content = self.objects[key]
        return StoredObject(
            key=key,
            content_type=content_type or self.content_types.get(
                key,
                "application/octet-stream",
            ),
            size_bytes=len(content),
            etag=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            metadata=self.metadata.get(key, {}),
        )


class FakeQueue:
    def __init__(self, storage: FakeStorage) -> None:
        self.storage = storage
        self.requests: dict[str, InferenceJobRequest] = {}
        self.statuses: dict[str, QueueJobStatus] = {}
        self.operations: list[tuple[str, str]] = []
        self.submit_counts: dict[str, int] = {}

    def submit(
        self,
        request: InferenceJobRequest,
        *,
        metadata: Mapping[str, str],
    ) -> QueueJobSnapshot:
        assert metadata["application_job_id"] == request.job_id
        count = self.submit_counts.get(request.job_id, 0) + 1
        self.submit_counts[request.job_id] = count
        transport_id = f"transport-{request.job_id}-{count}"
        self.requests[transport_id] = request
        self.statuses[transport_id] = QueueJobStatus.PENDING
        self.operations.append(("submit", transport_id))
        return QueueJobSnapshot(id=transport_id, status=QueueJobStatus.PENDING)

    def get(self, transport_job_id: str) -> QueueJobSnapshot:
        self.operations.append(("get", transport_job_id))
        status = self.statuses[transport_job_id]
        output = None
        if status is QueueJobStatus.SUCCEEDED:
            request = self.requests[transport_job_id]
            content = f"video:{request.job_id}".encode()
            self.storage.objects[request.output.key] = content
            digest = hashlib.sha256(content).hexdigest()
            self.storage.content_types[request.output.key] = request.output.content_type
            self.storage.metadata[request.output.key] = {
                "job-id": request.job_id,
                "request-sha256": request.fingerprint(),
                "artifact-sha256": digest,
            }
            output = InferenceJobResponse(
                job_id=request.job_id,
                request_sha256=request.fingerprint(),
                output=OutputArtifact(
                    key=request.output.key,
                    content_type="video/mp4",
                    size_bytes=len(content),
                    sha256=digest,
                    etag="etag",
                ),
                attempt_count=1,
                replayed=False,
            ).model_dump(mode="json")
        return QueueJobSnapshot(id=transport_job_id, status=status, output=output)

    def cancel(self, transport_job_id: str) -> QueueJobSnapshot:
        self.operations.append(("cancel", transport_job_id))
        self.statuses[transport_job_id] = QueueJobStatus.CANCELLED
        return QueueJobSnapshot(
            id=transport_job_id,
            status=QueueJobStatus.CANCELLED,
        )


def _png_bytes(value: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1280, 736), color=(value, value, value)).save(buffer, format="PNG")
    return buffer.getvalue()


def _inputs(tmp_path: Path):
    keyframes_dir = tmp_path / "phase6"
    assets_dir = keyframes_dir / "storyboard_keyframes"
    assets_dir.mkdir(parents=True)
    (assets_dir / "shot_001.png").write_bytes(_png_bytes(10))
    (assets_dir / "shot_002.png").write_bytes(_png_bytes(20))

    keyframes = [
        StoryboardKeyframe(shot_id=1, uri="storyboard_keyframes/shot_001.png"),
        StoryboardKeyframe(shot_id=2, uri="storyboard_keyframes/shot_002.png"),
    ]
    prompts = [
        VideoPrompt(shot_id=1, prompt="slow push in"),
        VideoPrompt(shot_id=2, prompt="gentle lateral drift"),
    ]
    timings = [
        ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.5),
        ShotTiming(shot_id=2, start_seconds=3.5, end_seconds=6.0),
    ]
    return keyframes_dir, keyframes, prompts, timings
