from __future__ import annotations

from pathlib import Path

import pytest
from _video_generation_support import FakeQueue, FakeStorage, _inputs

from ai_video_factory.providers.job_queue import QueueJobStatus
from ai_video_factory.workers.ltx25.model import LTX_PRODUCTION_I2V_PROFILE
from ai_video_factory.workflows.video_generation import (
    build_video_generation_plan,
    run_video_generation,
)


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
    assert first[0].request.parameters["generation_profile"] == LTX_PRODUCTION_I2V_PROFILE
    assert first[0].request.parameters["width"] == 1920
    assert first[0].request.parameters["height"] == 1080
    legacy = build_video_generation_plan(
        keyframes, prompts, timings, keyframe_base_dir=base,
        width=1280, height=720,
    )
    assert legacy[0].request.parameters["width"] == 1280
    assert legacy[0].request.parameters["height"] == 720
    assert legacy[0].request.job_id != first[0].request.job_id
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



def test_controlled_phase8_refuses_unreviewed_i2v_image_before_paid_capacity() -> None:
    source = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(
        encoding="utf-8"
    )
    assert "SALAD_LTX25_FP8_I2V_IMAGE" in source
    assert "Import-EnvFile" in source
    assert "image=$ExpectedFP8Image" in source
    assert source.index("image=$ExpectedFP8Image") < source.index("$WorkerManager @Start")
