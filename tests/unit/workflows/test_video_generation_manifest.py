from __future__ import annotations

from pathlib import Path

import pytest
from _video_generation_support import FakeQueue, FakeStorage, _inputs

import ai_video_factory.workflows.video_generation as video_generation
from ai_video_factory.domain import VideoPrompt
from ai_video_factory.workflows.video_generation import (
    VideoGenerationJobState,
    VideoGenerationManifest,
    build_video_generation_plan,
    run_video_generation,
)


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
