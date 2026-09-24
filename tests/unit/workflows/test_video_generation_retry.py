from __future__ import annotations

import json
from pathlib import Path

import pytest

from _video_generation_support import FakeQueue, FakeStorage, _inputs
from ai_video_factory.providers.job_queue import (
    QueueJobNotFoundError,
    QueueJobSnapshot,
    QueueJobStatus,
    TransientQueueError,
)
from ai_video_factory.workflows.video_generation import (
    VideoGenerationManifest,
    build_video_generation_plan,
    run_video_generation,
)


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
