from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from scripts.diagnostics.inspect_phase8_manifest import inspect_manifest_state

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.workflows.video_generation import (
    VideoGenerationJobState,
    VideoGenerationManifest,
    build_video_generation_plan,
    video_generation_run_fingerprint,
)


def _plan(tmp_path: Path):
    keyframe_dir = tmp_path / "phase6"
    asset_dir = keyframe_dir / "storyboard_keyframes"
    asset_dir.mkdir(parents=True)
    buffer = BytesIO()
    Image.new("RGB", (1536, 864)).save(buffer, format="PNG")
    (asset_dir / "shot_001.png").write_bytes(buffer.getvalue())
    keyframes = [
        StoryboardKeyframe(shot_id=1, uri="storyboard_keyframes/shot_001.png"),
    ]
    prompts = [VideoPrompt(shot_id=1, prompt="slow push in")]
    timings = [ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=3.0)]
    return build_video_generation_plan(
        keyframes,
        prompts,
        timings,
        keyframe_base_dir=keyframe_dir,
    )


def _write_manifest(
    path: Path,
    *,
    fingerprint: str,
    application_job_id: str,
    request_sha256: str,
    status: str,
    transport_job_id: str | None,
) -> None:
    manifest = VideoGenerationManifest(
        run_fingerprint=fingerprint,
        jobs=[
            VideoGenerationJobState(
                shot_id=1,
                application_job_id=application_job_id,
                request_sha256=request_sha256,
                transport_status=status,
                transport_job_id=transport_job_id,
                submission_count=1 if transport_job_id else 0,
            )
        ],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def test_missing_manifest_is_not_resume(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    state = inspect_manifest_state(
        plan,
        tmp_path / "phase8" / "video_generation_manifest.json",
    )

    assert state["status"] == "missing"
    assert state["active_resume_jobs"] == 0
    assert state["submitted_jobs"] == 0
    assert state["terminal_retry_jobs"] == 0


def test_matching_succeeded_manifest_is_not_active_resume(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan[0]
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    _write_manifest(
        manifest_path,
        fingerprint=video_generation_run_fingerprint(plan),
        application_job_id=item.request.job_id,
        request_sha256=item.request.fingerprint(),
        status="succeeded",
        transport_job_id="transport-old-success",
    )

    state = inspect_manifest_state(plan, manifest_path)

    assert state["status"] == "matching"
    assert state["submitted_jobs"] == 1
    assert state["active_resume_jobs"] == 0
    assert state["terminal_retry_jobs"] == 0


def test_matching_failed_manifest_is_terminal_retry(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan[0]
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    _write_manifest(
        manifest_path,
        fingerprint=video_generation_run_fingerprint(plan),
        application_job_id=item.request.job_id,
        request_sha256=item.request.fingerprint(),
        status="failed",
        transport_job_id="transport-failed",
    )

    state = inspect_manifest_state(plan, manifest_path)

    assert state["status"] == "matching"
    assert state["active_resume_jobs"] == 0
    assert state["terminal_retry_jobs"] == 1


def test_matching_running_manifest_is_active_resume(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan[0]
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    _write_manifest(
        manifest_path,
        fingerprint=video_generation_run_fingerprint(plan),
        application_job_id=item.request.job_id,
        request_sha256=item.request.fingerprint(),
        status="running",
        transport_job_id="transport-running",
    )

    state = inspect_manifest_state(plan, manifest_path)

    assert state["status"] == "matching"
    assert state["active_resume_jobs"] == 1
    assert state["terminal_retry_jobs"] == 0


def test_different_plan_manifest_can_be_archived_without_deleting_artifacts(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    _write_manifest(
        manifest_path,
        fingerprint="old-plan-fingerprint",
        application_job_id="old-application-job",
        request_sha256="old-request",
        status="succeeded",
        transport_job_id="transport-old",
    )

    inspected = inspect_manifest_state(plan, manifest_path)
    assert inspected["status"] == "different_plan"
    assert manifest_path.is_file()

    archived = inspect_manifest_state(
        plan,
        manifest_path,
        archive_mismatch=True,
    )

    assert archived["status"] == "archived_different_plan"
    assert not manifest_path.exists()
    archived_path = Path(str(archived["archived_path"]))
    assert archived_path.is_file()
    assert archived_path.parent.name == "manifest_archive"


def test_phase8_inspector_and_controlled_wrapper_pin_landscape_contract() -> None:
    inspector = Path("scripts/diagnostics/inspect_phase8_manifest.py").read_text(encoding="utf-8")
    wrapper = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert 'parser.add_argument("--width", type=int, default=1280)' in inspector
    assert 'parser.add_argument("--height", type=int, default=720)' in inspector
    assert (
        '"Phase 8 manifest inspection contract is exactly 1280x720 at 24 fps"'
        in inspector
    )
    assert wrapper.count("--width 1280 `") == 6
    assert wrapper.count("--height 720 `") == 6
    assert wrapper.count("--fps 24 `") == 6
    assert wrapper.count("--transport-route $env:SALAD_LTX25_QUEUE_NAME `") == 3



def test_transport_route_change_is_not_resumed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    item = plan[0]
    manifest_path = tmp_path / "phase8" / "video_generation_manifest.json"
    manifest = VideoGenerationManifest(
        run_fingerprint=video_generation_run_fingerprint(plan),
        transport_route="ai-video-factory-ltx25-jobs",
        jobs=[
            VideoGenerationJobState(
                shot_id=1,
                application_job_id=item.request.job_id,
                request_sha256=item.request.fingerprint(),
                transport_status="pending",
                transport_job_id="legacy-transport",
                submission_count=3,
            )
        ],
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    state = inspect_manifest_state(
        plan,
        manifest_path,
        transport_route="ai-video-factory-ltx25-jobs-v2",
    )

    assert state["status"] == "different_transport_route"
    assert state["active_resume_jobs"] == 1
    assert state["existing_transport_route"] == "ai-video-factory-ltx25-jobs"
    assert state["expected_transport_route"] == "ai-video-factory-ltx25-jobs-v2"

    archived = inspect_manifest_state(
        plan,
        manifest_path,
        archive_mismatch=True,
        transport_route="ai-video-factory-ltx25-jobs-v2",
    )

    assert archived["status"] == "archived_different_transport_route"
    assert not manifest_path.exists()
    assert Path(str(archived["archived_path"])).is_file()
