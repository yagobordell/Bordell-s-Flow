from __future__ import annotations

import json
from pathlib import Path

from _video_upscale_support import (
    FakeStorage,
    OutputCommittedBeforeTerminalFailureQueue,
    TransientThenSuccessQueue,
    _probe,
)

from ai_video_factory.domain import VideoClip
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus
from ai_video_factory.workflows import video_upscale as upscale
from scripts.diagnostics import inspect_phase8_upscale_manifest as upscale_inspector


def test_upscale_terminal_snapshot_preserves_provider_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    phase8 = tmp_path / "phase8"
    clips_dir = phase8 / "video_clips"
    clips_dir.mkdir(parents=True)
    source = clips_dir / "shot_001.mp4"
    source.write_bytes(b"source-ltx-720p")
    monkeypatch.setattr(upscale, "probe_video", _probe)

    item = upscale.build_video_upscale_plan(
        [VideoClip(shot_id=1, uri="video_clips/shot_001.mp4")],
        clip_base_dir=phase8,
    )[0]
    state = upscale.VideoUpscaleJobState(
        shot_id=1,
        application_job_id=item.request.job_id,
        request_sha256=item.request.fingerprint(),
    )
    payload = {
        "id": "transport-failed",
        "status": "failed",
        "events": [{"action": "rejected"}],
    }

    upscale._apply_snapshot(
        state,
        item,
        QueueJobSnapshot(
            id="transport-failed",
            status=QueueJobStatus.FAILED,
            provider_payload=payload,
        ),
    )

    assert state.last_terminal_transport_job_id == "transport-failed"
    assert state.last_terminal_payload == payload
    detail = upscale._terminal_failure_detail(state)
    assert "transport_job_id=transport-failed" in detail
    assert "provider_payload=" in detail
    assert "rejected" in detail


def test_upscale_manifest_inspector_counts_terminal_retry_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clips_path = tmp_path / "video_clips.json"
    clips_path.write_text(
        json.dumps([{"shot_id": 1, "uri": "video_clips/shot_001.mp4"}]),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "video_upscale_manifest.json"
    manifest = upscale.VideoUpscaleManifest(
        run_fingerprint="matching-fingerprint",
        jobs=[
            upscale.VideoUpscaleJobState(
                shot_id=1,
                application_job_id="phase8-upscale-001-test",
                request_sha256="request-sha",
                transport_job_id="transport-failed",
                transport_status="failed",
                submission_count=1,
            )
        ],
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    output_path = tmp_path / "inspection.json"

    monkeypatch.setattr(
        upscale_inspector,
        "build_video_upscale_plan",
        lambda *_a, **_k: [object()],
    )
    monkeypatch.setattr(
        upscale_inspector,
        "video_upscale_run_fingerprint",
        lambda _plan: "matching-fingerprint",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "inspect_phase8_upscale_manifest.py",
            "--clips",
            str(clips_path),
            "--manifest",
            str(manifest_path),
            "--json-output",
            str(output_path),
        ],
    )

    upscale_inspector.main()

    state = json.loads(output_path.read_text(encoding="utf-8"))
    assert state["status"] == "matching"
    assert state["active_resume_jobs"] == 0
    assert state["terminal_retry_jobs"] == 1


def test_upscale_polling_tolerates_transient_queue_get(
    tmp_path: Path,
    monkeypatch,
) -> None:
    phase8 = tmp_path / "phase8"
    clips_dir = phase8 / "video_clips"
    clips_dir.mkdir(parents=True)
    source = clips_dir / "shot_001.mp4"
    source.write_bytes(b"source-ltx-720p")
    monkeypatch.setattr(upscale, "probe_video", _probe)

    plan = upscale.build_video_upscale_plan(
        [VideoClip(shot_id=1, uri="video_clips/shot_001.mp4")],
        clip_base_dir=phase8,
    )
    storage = FakeStorage()
    queue = TransientThenSuccessQueue(storage)

    manifest, clips = upscale.run_video_upscale(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=phase8 / "video_upscale_manifest.json",
        clips_dir=phase8 / "upscaled_clips",
        poll_seconds=0.001,
        timeout_seconds=1.0,
        dispatch_timeout_seconds=0.002,
    )

    assert queue.get_calls >= 2
    assert queue.cancel_calls == 0
    assert manifest.jobs[0].transport_status == "succeeded"
    assert clips == [VideoClip(shot_id=1, uri="upscaled_clips/shot_001.mp4")]


def test_verified_r2_output_wins_over_terminal_transport_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    phase8 = tmp_path / "phase8"
    clips_dir = phase8 / "video_clips"
    clips_dir.mkdir(parents=True)
    source = clips_dir / "shot_001.mp4"
    source.write_bytes(b"source-ltx-720p")
    monkeypatch.setattr(upscale, "probe_video", _probe)

    plan = upscale.build_video_upscale_plan(
        [VideoClip(shot_id=1, uri="video_clips/shot_001.mp4")],
        clip_base_dir=phase8,
    )
    storage = FakeStorage()
    queue = OutputCommittedBeforeTerminalFailureQueue(storage)

    manifest, clips = upscale.run_video_upscale(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=phase8 / "video_upscale_manifest.json",
        clips_dir=phase8 / "upscaled_clips",
        poll_seconds=0.001,
        timeout_seconds=1.0,
    )

    assert queue.get_calls == 1
    assert manifest.jobs[0].transport_status == "succeeded"
    assert manifest.jobs[0].response is not None
    assert manifest.jobs[0].response.replayed is True
    assert clips == [VideoClip(shot_id=1, uri="upscaled_clips/shot_001.mp4")]
