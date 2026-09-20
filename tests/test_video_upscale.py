from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ai_video_factory.compositor.media import MediaProbe
from ai_video_factory.domain import VideoClip
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus
from ai_video_factory.workers.realesrgan import (
    REALESRGAN_GENERATION_PROFILE,
    REALESRGAN_MODEL_NAME,
    RealESRGANParameters,
    realesrgan_application_job_id,
)
from ai_video_factory.workers.realesrgan.model import DirectRealESRGANBackend
from ai_video_factory.workflows import video_upscale as upscale
from scripts import inspect_phase8_upscale_manifest as upscale_inspector


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


def test_upscale_plan_is_exact_720p_to_1440p_and_cache_skips_queue(
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
    item = plan[0]
    assert item.request.parameters["source_width"] == 1280
    assert item.request.parameters["source_height"] == 720
    assert item.request.parameters["target_width"] == 2560
    assert item.request.parameters["target_height"] == 1440
    assert item.request.parameters["fps"] == 24
    assert item.request.parameters["model_name"] == REALESRGAN_MODEL_NAME

    storage = FakeStorage()
    output_data = b"cached-upscaled-1440p"
    output_sha = hashlib.sha256(output_data).hexdigest()
    storage.objects[item.request.output.key] = (
        output_data,
        StoredObject(
            key=item.request.output.key,
            content_type="video/mp4",
            size_bytes=len(output_data),
            etag="cached",
            metadata={
                "job-id": item.request.job_id,
                "request-sha256": item.request.fingerprint(),
                "artifact-sha256": output_sha,
            },
        ),
    )
    queue = NoSubmitQueue()

    manifest, clips = upscale.run_video_upscale(
        plan,
        queue=queue,
        storage=storage,
        manifest_path=phase8 / "video_upscale_manifest.json",
        clips_dir=phase8 / "upscaled_clips",
        poll_seconds=0.01,
        timeout_seconds=1.0,
    )

    assert queue.submissions == 0
    assert manifest.jobs[0].transport_status == "succeeded"
    assert manifest.jobs[0].response is not None
    assert manifest.jobs[0].response.replayed is True
    assert clips == [VideoClip(shot_id=1, uri="upscaled_clips/shot_001.mp4")]


def test_upscale_job_identity_invalidates_on_source_sha_and_profile() -> None:
    common = dict(
        shot_id=1,
        source_width=1280,
        source_height=720,
        source_frame_count=24,
        fps=24,
        target_width=2560,
        target_height=1440,
    )
    first = realesrgan_application_job_id(source_sha256="a" * 64, **common)
    source_changed = realesrgan_application_job_id(source_sha256="b" * 64, **common)
    profile_changed = realesrgan_application_job_id(
        source_sha256="a" * 64,
        generation_profile="future-profile-v2",
        **common,
    )

    assert first != source_changed
    assert first != profile_changed


def test_realesrgan_contract_rejects_non_exact_two_x_target() -> None:
    payload = {
        "generation_profile": REALESRGAN_GENERATION_PROFILE,
        "model_name": REALESRGAN_MODEL_NAME,
        "source_sha256": "a" * 64,
        "source_width": 1280,
        "source_height": 720,
        "source_frame_count": 24,
        "target_width": 2048,
        "target_height": 1080,
        "fps": 24,
    }

    try:
        RealESRGANParameters.model_validate(payload)
    except ValueError as exc:
        assert "exactly 2x" in str(exc)
    else:
        raise AssertionError("non-2x target unexpectedly accepted")


def test_realesrgan_salad_service_scales_to_zero() -> None:
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["realesrgan"]

    assert document["stack"]["service_order"][-1] == "realesrgan"
    assert service["queue_name"] == "ai-video-factory-realesrgan-jobs"
    assert service["resources"]["gpu_class_names"] == ["RTX 3090 (24 GB)"]
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 2


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


def test_realesrgan_readiness_does_not_wait_for_inference_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend = DirectRealESRGANBackend(model_root=tmp_path)
    backend._upsampler = object()
    monkeypatch.setattr(backend, "_validate_bootstrap", lambda: None)

    class ExplodingLock:
        def __enter__(self):
            raise AssertionError("ready() must not acquire the long-running inference lock")

        def __exit__(self, *_args):
            return None

    backend._lock = ExplodingLock()  # type: ignore[assignment]
    backend.ready()


def test_realesrgan_worker_emits_frame_progress_and_clears_cuda_cache() -> None:
    model = Path("src/ai_video_factory/workers/realesrgan/model.py").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))

    assert "REALESRGAN_PROGRESS" in model
    assert "frame_count % 10 == 0" in model
    assert "cuda.memory_allocated()" in model
    assert "cuda.memory_reserved()" in model
    assert "cuda.mem_get_info()" in model
    assert "cuda.empty_cache()" in model
    assert manifest["services"]["realesrgan"]["image"].endswith(
        ":realesrgan-x2plus-v2"
    )


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

    monkeypatch.setattr(upscale_inspector, "build_video_upscale_plan", lambda *_a, **_k: [object()])
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
