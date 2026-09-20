from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import ai_video_factory.workflows.video_generation as video_generation
from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.gpu.contracts import GPUJobRequest, GPUJobResponse, OutputArtifact
from ai_video_factory.gpu.ports import StoredObject
from ai_video_factory.providers.job_queue import (
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)
from ai_video_factory.workflows.video_generation import (
    VideoGenerationIncompleteError,
    VideoGenerationJobState,
    VideoGenerationManifest,
    build_video_generation_plan,
    run_video_generation,
)


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
        self.requests: dict[str, GPUJobRequest] = {}
        self.statuses: dict[str, QueueJobStatus] = {}
        self.operations: list[tuple[str, str]] = []
        self.submit_counts: dict[str, int] = {}

    def submit(
        self,
        request: GPUJobRequest,
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
            output = GPUJobResponse(
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
    Image.new("RGB", (1536, 864), color=(value, value, value)).save(buffer, format="PNG")
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


def test_plan_is_deterministic_and_preserves_ltx_frame_rules(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)

    first = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    second = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )

    assert [item.request.job_id for item in first] == [item.request.job_id for item in second]
    assert [item.request.parameters["num_frames"] for item in first] == [89, 65]
    assert [item.request.parameters["seed"] for item in first] == [43, 44]
    assert first[0].request.parameters["width"] == 1280
    assert first[0].request.parameters["height"] == 720
    assert first[0].request.parameters["fps"] == 24
    assert first[0].request.output.key.endswith("/shot_001.mp4")
    assert first[1].request.output.key.endswith("/shot_002.mp4")


def test_plan_rejects_misaligned_inputs(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    prompts.reverse()

    with pytest.raises(ValueError, match="prompts must match"):
        build_video_generation_plan(
            keyframes,
            prompts,
            timings,
            keyframe_base_dir=base,
        )


def test_fanout_submits_all_jobs_before_polling_and_resume_skips_successes(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    clips_dir = tmp_path / "phase8" / "video_clips"

    manifest, clips = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )
    assert clips == []
    assert [op for op, _ in queue.operations] == ["submit", "submit"]
    assert all(state.transport_status == "pending" for state in manifest.jobs)

    for transport_id in list(queue.statuses):
        queue.statuses[transport_id] = QueueJobStatus.SUCCEEDED

    queue.operations.clear()
    manifest, clips = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        poll_seconds=0.001,
        timeout_seconds=1,
    )

    assert [clip.shot_id for clip in clips] == [1, 2]
    assert [clip.uri for clip in clips] == [
        "video_clips/shot_001.mp4",
        "video_clips/shot_002.mp4",
    ]
    assert all(state.transport_status == "succeeded" for state in manifest.jobs)
    assert sum(queue.submit_counts.values()) == 2
    assert all((clips_dir / f"shot_{shot_id:03d}.mp4").is_file() for shot_id in (1, 2))

    queue.operations.clear()
    run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        poll_seconds=0.001,
        timeout_seconds=1,
    )
    assert sum(queue.submit_counts.values()) == 2
    assert queue.operations == []



def test_polling_tolerates_transient_queue_get_without_cancelling_jobs(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    original_get = queue.get
    transient_raised = False

    def flaky_get(transport_job_id: str) -> QueueJobSnapshot:
        nonlocal transient_raised
        if not transient_raised:
            transient_raised = True
            raise TransientQueueError("temporary Salad control-plane reset")
        queue.statuses[transport_job_id] = QueueJobStatus.SUCCEEDED
        return original_get(transport_job_id)

    queue.get = flaky_get  # type: ignore[method-assign]

    manifest, clips = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=tmp_path / "manifest.json",
        clips_dir=tmp_path / "clips",
        poll_seconds=0.001,
        timeout_seconds=1.0,
        dispatch_timeout_seconds=0.01,
    )

    assert transient_raised
    assert [clip.shot_id for clip in clips] == [1, 2]
    assert all(state.transport_status == "succeeded" for state in manifest.jobs)
    assert [op for op, _ in queue.operations].count("cancel") == 0
    assert sum(queue.submit_counts.values()) == 2


def test_resume_preserves_transport_on_transient_status_read(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"
    clips_dir = tmp_path / "clips"

    first, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )
    original_transport_ids = [state.transport_job_id for state in first.jobs]
    original_get = queue.get
    transient_raised = False

    def flaky_get(transport_job_id: str) -> QueueJobSnapshot:
        nonlocal transient_raised
        if not transient_raised:
            transient_raised = True
            raise TransientQueueError("temporary Salad control-plane reset")
        return original_get(transport_job_id)

    queue.get = flaky_get  # type: ignore[method-assign]

    resumed, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )

    assert transient_raised
    assert [state.transport_job_id for state in resumed.jobs] == original_transport_ids
    assert [state.submission_count for state in resumed.jobs] == [1, 1]

def test_first_dispatch_timeout_cancels_stuck_pending_transports(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"

    with pytest.raises(TimeoutError, match="did not dispatch any queued job"):
        run_video_generation(
            plan,
            queue=queue,
            storage=storage,
            manifest_path=manifest_path,
            clips_dir=tmp_path / "phase8" / "video_clips",
            poll_seconds=0.001,
            timeout_seconds=1.0,
            dispatch_timeout_seconds=0.002,
        )

    assert [op for op, _ in queue.operations].count("cancel") == 2
    saved = VideoGenerationManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    assert all(state.transport_status == "cancelled" for state in saved.jobs)


def test_resume_resubmits_purged_transport_with_same_application_id(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"
    clips_dir = tmp_path / "clips"

    manifest, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )
    purged_transport = manifest.jobs[0].transport_job_id
    retained_transport = manifest.jobs[1].transport_job_id
    assert purged_transport is not None
    assert retained_transport is not None

    original_get = queue.get

    def get_with_purged_transport(transport_job_id: str) -> QueueJobSnapshot:
        if transport_job_id == purged_transport:
            raise QueueJobNotFoundError("transport was purged")
        return original_get(transport_job_id)

    queue.get = get_with_purged_transport  # type: ignore[method-assign]

    resumed, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )

    assert resumed.jobs[0].submission_count == 2
    assert resumed.jobs[0].application_job_id == plan[0].request.job_id
    assert resumed.jobs[0].transport_job_id != purged_transport
    assert resumed.jobs[0].transport_status == "pending"
    assert resumed.jobs[1].submission_count == 1
    assert resumed.jobs[1].transport_job_id == retained_transport


def test_resume_resubmits_only_terminal_failure_with_same_application_id(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"
    clips_dir = tmp_path / "clips"

    manifest, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )
    first_transport = manifest.jobs[0].transport_job_id
    second_transport = manifest.jobs[1].transport_job_id
    assert first_transport is not None
    assert second_transport is not None

    queue.statuses[first_transport] = QueueJobStatus.SUCCEEDED
    queue.statuses[second_transport] = QueueJobStatus.FAILED

    resumed, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        wait=False,
    )

    assert resumed.jobs[0].submission_count == 1
    assert resumed.jobs[0].transport_status == "succeeded"
    assert resumed.jobs[1].submission_count == 2
    assert resumed.jobs[1].application_job_id == plan[1].request.job_id
    assert resumed.jobs[1].transport_job_id != second_transport


def test_terminal_failure_is_persisted_for_a_later_resume(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"

    manifest, _ = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=tmp_path / "clips",
        wait=False,
    )
    for state in manifest.jobs:
        assert state.transport_job_id is not None
        queue.statuses[state.transport_job_id] = QueueJobStatus.FAILED

    with pytest.raises(VideoGenerationIncompleteError, match="Rerun to resume"):
        run_video_generation(
            plan,
            queue=queue,
            storage=storage,
            manifest_path=manifest_path,
            clips_dir=tmp_path / "clips",
            retry_terminal=False,
            poll_seconds=0.001,
            timeout_seconds=1,
        )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["transport_status"] for item in saved["jobs"]] == ["failed", "failed"]


def test_manifest_rejects_changed_generation_plan(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"

    run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=tmp_path / "clips",
        wait=False,
    )

    changed_prompts = list(prompts)
    changed_prompts[0] = VideoPrompt(shot_id=1, prompt="a different motion plan")
    changed = build_video_generation_plan(
        keyframes,
        changed_prompts,
        timings,
        keyframe_base_dir=base,
    )

    with pytest.raises(ValueError, match="different input plan"):
        run_video_generation(
            changed,
            queue=queue,
            storage=storage,
            manifest_path=manifest_path,
            clips_dir=tmp_path / "clips",
            wait=False,
        )




def test_transport_route_change_archives_queue_local_resume_state(
    tmp_path: Path,
) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    old_queue = FakeQueue(storage)
    manifest_path = tmp_path / "manifest.json"

    old_manifest, _ = run_video_generation(
        plan,
        queue=old_queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=tmp_path / "clips",
        wait=False,
        transport_route="old-queue",
    )
    assert all(state.transport_status == "pending" for state in old_manifest.jobs)

    new_queue = FakeQueue(storage)
    migrated, _ = run_video_generation(
        plan,
        queue=new_queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=tmp_path / "clips",
        wait=False,
        transport_route="new-queue",
    )

    assert migrated.transport_route == "new-queue"
    assert [state.submission_count for state in migrated.jobs] == [1, 1]
    assert sum(new_queue.submit_counts.values()) == 2
    assert list(tmp_path.glob("manifest.archive-*.json"))


def test_manifest_replace_retries_transient_windows_sharing_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "video_generation_manifest.json"
    manifest = VideoGenerationManifest(
        run_fingerprint="fingerprint",
        jobs=[
            VideoGenerationJobState(
                shot_id=1,
                application_job_id="job-1",
                request_sha256="request-1",
            )
        ],
    )
    original_replace = video_generation.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "Access is denied")
        original_replace(source, destination)

    monkeypatch.setattr(video_generation.os, "replace", flaky_replace)
    monkeypatch.setattr(video_generation.time, "sleep", lambda _: None)

    video_generation._write_manifest(manifest_path, manifest)

    assert attempts == 3
    saved = VideoGenerationManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    assert saved == manifest



def test_r2_replay_is_resolved_before_any_queue_operation(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    for item in plan:
        content = f"cached:{item.request.job_id}".encode()
        digest = hashlib.sha256(content).hexdigest()
        storage.objects[item.request.output.key] = content
        storage.content_types[item.request.output.key] = "video/mp4"
        storage.metadata[item.request.output.key] = {
            "job-id": item.request.job_id,
            "request-sha256": item.request.fingerprint(),
            "artifact-sha256": digest,
        }

    manifest, clips = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=tmp_path / "manifest.json",
        clips_dir=tmp_path / "clips",
        poll_seconds=0.001,
        timeout_seconds=1,
    )

    assert queue.operations == []
    assert queue.submit_counts == {}
    assert [clip.shot_id for clip in clips] == [1, 2]
    assert all(state.submission_count == 0 for state in manifest.jobs)
    assert all(state.response is not None for state in manifest.jobs)
    assert all(state.response.replayed for state in manifest.jobs if state.response)


def test_invalid_r2_replay_metadata_fails_before_queue_submission(tmp_path: Path) -> None:
    base, keyframes, prompts, timings = _inputs(tmp_path)
    plan = build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=base,
    )
    storage = FakeStorage()
    queue = FakeQueue(storage)
    item = plan[0]
    content = b"corrupt-metadata"
    storage.objects[item.request.output.key] = content
    storage.content_types[item.request.output.key] = "video/mp4"
    storage.metadata[item.request.output.key] = {
        "job-id": item.request.job_id,
        "request-sha256": "0" * 64,
        "artifact-sha256": hashlib.sha256(content).hexdigest(),
    }

    with pytest.raises(RuntimeError, match="metadata does not match"):
        run_video_generation(
            plan,
            queue=queue,
            storage=storage,
            manifest_path=tmp_path / "manifest.json",
            clips_dir=tmp_path / "clips",
            wait=False,
        )

    assert queue.operations == []
    assert queue.submit_counts == {}
