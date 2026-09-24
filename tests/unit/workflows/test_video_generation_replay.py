from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _video_generation_support import FakeQueue, FakeStorage, _inputs

from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus
from ai_video_factory.workflows.video_generation import (
    VideoGenerationIncompleteError,
    build_video_generation_plan,
    run_video_generation,
)


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
    terminal_payloads: dict[str, dict[str, object]] = {}
    for state in manifest.jobs:
        assert state.transport_job_id is not None
        queue.statuses[state.transport_job_id] = QueueJobStatus.FAILED
        terminal_payloads[state.transport_job_id] = {
            "id": state.transport_job_id,
            "status": "failed",
            "events": [{"action": "rejected"}],
        }

    original_get = queue.get

    def failed_get(transport_job_id: str) -> QueueJobSnapshot:
        snapshot = original_get(transport_job_id)
        return QueueJobSnapshot(
            id=snapshot.id,
            status=snapshot.status,
            output=snapshot.output,
            provider_payload=terminal_payloads[transport_job_id],
        )

    queue.get = failed_get  # type: ignore[method-assign]

    with pytest.raises(
        VideoGenerationIncompleteError,
        match=r"transport_job_id=.*provider_payload=.*rejected",
    ):
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
    assert all(item["last_terminal_transport_job_id"] for item in saved["jobs"])
    assert all(item["last_terminal_payload"]["status"] == "failed" for item in saved["jobs"])


def test_verified_r2_output_wins_over_terminal_transport_failure(
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

    # Simulate the worker having durably committed the MP4 before Salad reports
    # the corresponding transport as terminally failed.
    queue.statuses[first_transport] = QueueJobStatus.SUCCEEDED
    queue.get(first_transport)
    queue.statuses[first_transport] = QueueJobStatus.PENDING
    queue.statuses[second_transport] = QueueJobStatus.SUCCEEDED
    original_get = queue.get
    output_key = plan[0].request.output.key
    hide_first_output = True
    first_reads = 0
    original_stat = storage.stat

    def raced_stat(key: str) -> StoredObject | None:
        if key == output_key and hide_first_output:
            return None
        return original_stat(key)

    storage.stat = raced_stat  # type: ignore[method-assign]

    def raced_get(transport_job_id: str) -> QueueJobSnapshot:
        nonlocal first_reads, hide_first_output
        if transport_job_id == first_transport and first_reads == 1:
            hide_first_output = False
            queue.statuses[first_transport] = QueueJobStatus.FAILED
        if transport_job_id == first_transport:
            first_reads += 1
        return original_get(transport_job_id)

    queue.get = raced_get  # type: ignore[method-assign]

    resumed, clips = run_video_generation(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=manifest_path,
        clips_dir=clips_dir,
        retry_terminal=False,
        poll_seconds=0.001,
        timeout_seconds=1,
    )

    assert [clip.shot_id for clip in clips] == [1, 2]
    assert all(state.transport_status == "succeeded" for state in resumed.jobs)
    assert [state.submission_count for state in resumed.jobs] == [1, 1]
    assert first_reads == 2


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
