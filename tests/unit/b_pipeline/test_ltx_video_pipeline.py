"""Offline contract tests for B2 -> LTX grouped A2V / individual I2V."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
    LTX_A2V_RUN_OPTIMIZED_PROFILE,
    LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
    LTX_A2V_RUN_VAE_BACKEND,
    LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
    LTX_A2V_STATIC_SAFE_V4_GENERATION_PROFILE,
    LTXAudioToVideoParameters,
)
from ai_video_factory.workers.ltx25.model import (
    LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
    LTX_GENERATION_PROFILE,
    LTX_VIDEO_TASK,
    LTXVideoParameters,
)
from ai_video_factory.workflows import b_video
from scripts.pipeline import run_b_pipeline as runner


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict, dict]:
    avatar = tmp_path / "avatar.png"
    Image.new("RGB", (1280, 720), (230, 20, 20)).save(avatar)
    images = {"status": "completed", "items": []}
    for name, color in (
        ("1B", (30, 230, 30)),
        ("1D", (30, 30, 230)),
        ("1E", (230, 230, 30)),
    ):
        path = tmp_path / "images" / f"{name}.png"
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (1280, 720), color).save(path)
        images["items"].append({
            "block_id": 1, "beat_id": name,
            "file": path.relative_to(tmp_path).as_posix(), "sha256": _sha(path),
        })
    master = tmp_path / "audio" / "narration.wav"
    master.parent.mkdir()
    with wave.open(str(master), "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(48000)
        sound.writeframes(bytes(3 * 48000 * 2))
    audio = {
        "file": "audio/narration.wav", "sha256": _sha(master),
        "status": "completed", "stt_complete": True,
        "duration_seconds": 3.0, "stt_model": "whisper-1",
    }
    plan = {
        "avatar": {"file": "avatar.png", "sha256": _sha(avatar)},
        "blocks": [],
    }
    definitions = [
        ("1A", "avatar", None, 0.0, 0.4),
        ("1B", "avatar_media", "side-by-side picture", 0.4, 0.9),
        ("1C", "avatar", None, 0.9, 1.4),
        ("1D", "media_video", "A bird takes off.", 1.4, 2.0),
        ("1E", "media_image", "A beautiful sunset.", 2.0, 2.6),
        ("2A", "avatar", None, 2.6, 3.0),
    ]
    timeline = {
        "duration_seconds": 3.0, "audio_file": audio["file"],
        "audio_sha256": audio["sha256"], "fps": 30,
        "beats": [
            {
                "beat_id": name, "block_id": int(name[0]),
                "visual_type": kind, "description": description,
                "start_seconds": start, "end_seconds": end,
                "duration_seconds": end - start,
            }
            for name, kind, description, start, end in definitions
        ],
    }
    # STT alignment is covered by test_openai_stt_preview. Pin the computed
    # timeline here to test the GPU job grouping independently of Whisper.
    monkeypatch.setattr(
        b_video, "build_stt_beat_timeline",
        lambda *_args: copy.deepcopy(timeline),
    )
    return plan, audio, images


def test_final_compositor_uses_professional_finished_asset_identity() -> None:
    from ai_video_factory.compositor import b_video as compositor

    original = {
        "block_id": 2,
        "beat_id": "2A",
        "file": "images/block_2/2A.png",
        "sha256": "a" * 64,
    }
    finished = {
        **original,
        "sha256": "b" * 64,
        "provider": "image_x2",
        "source_sha256": original["sha256"],
    }
    images = {
        "items": [original],
        "panel_assets": [],
        "finishing_status": "completed",
        "final_assets": [finished],
    }

    assert compositor._image_records(images) == [finished]


def test_final_compositor_alternates_media_image_zoom_direction() -> None:
    from ai_video_factory.compositor import b_video as compositor

    first: dict[str, object] = {}
    second: dict[str, object] = {}
    first_filter = compositor._motion_filter(first, 24, 0)
    second_filter = compositor._motion_filter(second, 24, 1)

    assert first["motion"] == {
        "version": "media-image-subpixel-affine-v3",
        "mode": "ken_burns",
        "direction": "out",
        "origin": "bottom_left",
        "zoom_start": 1.02,
        "zoom_end": 1.0,
    }
    assert second["motion"] == {
        "version": "media-image-subpixel-affine-v3",
        "mode": "ken_burns",
        "direction": "in",
        "origin": "bottom_left",
        "zoom_start": 1.0,
        "zoom_end": 1.02,
    }
    assert "1.020000+(1.000000-1.020000)" in first_filter
    assert "1.000000+(1.020000-1.000000)" in second_filter


def test_avatar_runs_share_one_audio_and_use_exact_pinned_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    _timeline, jobs, _run_dir = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    assert [(job.kind, job.beats) for job in jobs] == [
        ("a2v", ("1A", "1B", "1C")),
        ("i2v", ("1D",)),
        ("a2v", ("2A",)),
    ]
    first, video, last = jobs
    assert first.request.parameters["prompt"] == "A Person speaking to the camera."
    assert last.request.parameters["prompt"] == first.request.parameters["prompt"]
    assert first.request.parameters["generation_profile"] == (
        LTX_A2V_RUN_OPTIMIZED_PROFILE
    )
    assert first.request.parameters["vae_backend"] == LTX_A2V_RUN_VAE_BACKEND
    assert first.request.parameters["guidance_forward_batch_size"] == (
        LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE
    )
    assert first.request.parameters["progressive_block_group_size"] == (
        LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE
    )
    assert b_video.AVATAR_NEGATIVE_PROMPT == (
        "camera zoom, zoom in, push in, dolly in, camera drift, camera shake, "
        "reframing, crop change, handheld camera, unintended camera movement"
    )
    assert len(first.request.inputs) == 2
    assert {item.name for item in first.request.inputs} == {"image", "audio"}
    assert video.request.task == LTX_VIDEO_TASK
    assert video.request.job_id.startswith("b2-1D-")
    assert video.request.parameters["generation_profile"] == (
        b_video.LTX_PRODUCTION_I2V_PROFILE
    )
    assert video.request.max_attempts == 2
    assert video.request.parameters["prompt"] == "A bird takes off."
    assert video.request.parameters["num_frames"] == 17
    old_no_offload_job_id = "b2-1D-" + b_video.ltx_video_application_job_id(
        shot_id=2,
        prompt=video.request.parameters["prompt"],
        keyframe_sha256=video.request.inputs[0].sha256,
        seed=video.request.parameters["seed"],
        width=video.request.parameters["width"],
        height=video.request.parameters["height"],
        fps=video.request.parameters["fps"],
        num_frames=video.request.parameters["num_frames"],
        generation_profile=LTX_FAST_FULLRES_NO_OFFLOAD_PROFILE,
    )
    assert video.request.job_id != old_no_offload_job_id
    assert _run_dir.name
    for job in jobs:
        assert "run_id" not in job.request.parameters
        assert "run_job_count" not in job.request.parameters
        if job.kind == "a2v":
            LTXAudioToVideoParameters.model_validate(job.request.parameters)
        else:
            LTXVideoParameters.model_validate(job.request.parameters)
        assert job.request.parameters["width"] == 1920
        assert job.request.parameters["height"] == 1080
        assert job.request.parameters["fps"] == 24
    assert video.request.inputs[0].sha256 == images["items"][1]["sha256"]
    assert first.duration_seconds == pytest.approx(1.4)
    assert last.duration_seconds == pytest.approx(0.4)
    with wave.open(str(first.audio), "rb") as speech:
        assert speech.getnframes() == 67200
        assert speech.getframerate() == 48000
    with wave.open(str(last.audio), "rb") as speech:
        assert speech.getnframes() == 19200
    assert len(list((first.audio.parent).glob("*.wav"))) == 2


def test_long_consecutive_avatar_run_fails_before_paid_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        b_video, "build_stt_beat_timeline",
        lambda *_args: {
            "duration_seconds": 60.0,
            "beats": [{
                "beat_id": "1A", "block_id": 1, "visual_type": "avatar",
                "description": None, "start_seconds": 0.0, "end_seconds": 60.0,
            }],
        },
    )
    with pytest.raises(b_video.B2VideoError, match="single A2V"):
        b_video.build_b2_video_plan(tmp_path, plan, audio, images)
    assert not list((tmp_path / "video").rglob("*.wav"))



def test_unrepresentable_24fps_beat_is_rejected_before_paid_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        b_video, "build_stt_beat_timeline",
        lambda *_args: {
            "duration_seconds": 3.0,
            "beats": [
                {
                    "beat_id": "1A", "block_id": 1, "visual_type": "avatar",
                    "description": None, "start_seconds": 0.0,
                    "end_seconds": 0.01,
                },
                {
                    "beat_id": "1B", "block_id": 1, "visual_type": "avatar_media",
                    "description": "picture", "start_seconds": 0.01,
                    "end_seconds": 3.0,
                },
            ],
        },
    )
    with pytest.raises(b_video.B2VideoError, match="cannot be cut exactly"):
        b_video.build_b2_video_plan(tmp_path, plan, audio, images)
    assert not (tmp_path / "video" / "runs").exists()


def test_automatic_ltx_activation_requires_complete_audio_and_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    report = {"run": {"audio_generation": audio}}
    monkeypatch.setattr(runner.settings, "postgres_dsn", None)
    assert asyncio.run(
        runner._video_if_ready(tmp_path, report, plan=plan, images=images)
    ) == {"status": "deferred", "reason": "ltx_transport_not_configured"}
    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        asyncio.run(
            runner._video_if_ready(
                tmp_path, report, plan=plan, images=images, require=True
            )
        )
    monkeypatch.setattr(runner.settings, "postgres_dsn", "postgres://test")
    monkeypatch.setattr(runner.settings, "r2_endpoint_url", "https://r2.invalid")
    monkeypatch.setattr(runner.settings, "r2_bucket", "test")
    monkeypatch.setattr(runner.settings, "r2_access_key_id", "test")
    monkeypatch.setattr(runner.settings, "r2_secret_access_key", "test")
    closed: list[bool] = []
    calls: list[tuple[object, ...]] = []

    class FakeQueue:
        def __init__(self, *, dsn: str) -> None:
            assert dsn == "postgres://test"

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runner, "PostgresJobQueueClient", FakeQueue)
    monkeypatch.setattr(runner, "create_r2_storage", lambda **_kwargs: "storage")

    def dispatch(
        output: Path, selected_plan: dict, selected_audio: dict,
        selected_images: dict, **kwargs: object,
    ) -> dict:
        calls.append((output, selected_plan, selected_audio, selected_images, kwargs))
        return {"status": "completed", "file": "video/final.mp4"}

    monkeypatch.setattr(runner, "generate_b2_ltx_video", dispatch)
    checked: list[str] = []
    monkeypatch.setattr(
        runner, "_require_ltx_capacity_controller",
        lambda: checked.append("checked"),
    )
    result = asyncio.run(
        runner._video_if_ready(tmp_path, report, plan=plan, images=images)
    )
    assert result == {"status": "completed", "file": "video/final.mp4"}
    assert len(calls) == 1
    assert calls[0][:4] == (tmp_path, plan, audio, images)
    assert calls[0][4]["storage"] == "storage"
    calls[0][4]["check_capacity"]()
    assert checked == ["checked"]
    assert closed == [True]



def test_ltx_runtime_env_precedence_matches_application_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SALAD_LTX25_FP8_I2V_IMAGE", raising=False)

    def fake_dotenv_values(path: Path) -> dict[str, str]:
        path = Path(path)
        if path.name == ".env":
            return {"SALAD_LTX25_FP8_I2V_IMAGE": "base"}
        if path.name == "app.env":
            return {"SALAD_LTX25_FP8_I2V_IMAGE": "app"}
        if path.name == "salad.env":
            return {"SALAD_LTX25_FP8_I2V_IMAGE": "salad"}
        return {}

    monkeypatch.setattr(runner, "dotenv_values", fake_dotenv_values)
    assert runner._runtime_env_value("SALAD_LTX25_FP8_I2V_IMAGE") == "salad"

    monkeypatch.setenv("SALAD_LTX25_FP8_I2V_IMAGE", "process")
    assert runner._runtime_env_value("SALAD_LTX25_FP8_I2V_IMAGE") == "process"



def test_i2v_worker_guard_uses_runtime_env_file_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = "docker.io/yagobordell/ai-video-factory@sha256:" + "c" * 64
    values = {
        "SALAD_LTX25_FP8_I2V_IMAGE": pinned,
        "SALAD_API_BASE_URL": "https://salad.invalid/api/public",
        "SALAD_ORGANIZATION": "env-file-org",
        "SALAD_PROJECT": "env-file-project",
    }
    monkeypatch.setattr(runner.settings, "salad_api_key", "test-key")
    monkeypatch.delenv("SALAD_ORGANIZATION", raising=False)
    monkeypatch.delenv("SALAD_PROJECT", raising=False)
    monkeypatch.delenv("SALAD_API_BASE_URL", raising=False)
    monkeypatch.setattr(
        runner, "_runtime_env_value", lambda name: values.get(name, "")
    )

    class FakeSaladClient:
        def __init__(self, config: object) -> None:
            assert config.api_key == "test-key"
            assert config.api_base_url == "https://salad.invalid/api/public"
            assert config.organization_name == "env-file-org"
            assert config.project_name == "env-file-project"
            assert config.container_group_name == (
                "ai-video-factory-ltx25-i2v-worker-v1"
            )

        def describe_container_group(self) -> dict:
            return {"container": {"image": pinned}}

    monkeypatch.setattr(runner, "SaladClient", FakeSaladClient)

    runner._require_ltx_safe_i2v_worker()



def test_promoted_a2v_run_v2_requires_exact_new_deployed_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.settings, "salad_api_key", "test-key")
    monkeypatch.setenv("SALAD_ORGANIZATION", "org")
    monkeypatch.setenv("SALAD_PROJECT", "project")
    monkeypatch.delenv("SALAD_LTX25_RUN_V2_IMAGE", raising=False)
    monkeypatch.delenv("SALAD_LTX25_RUN_STATIC_FP8_IMAGE", raising=False)
    monkeypatch.delenv("SALAD_LTX25_PAGEABLE_A2V_IMAGE", raising=False)

    with pytest.raises(RuntimeError, match="SALAD_LTX25_RUN_V2_IMAGE"):
        runner._require_ltx_safe_a2v_worker()

    pinned = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64
    old_image = "docker.io/yagobordell/ai-video-factory@sha256:" + "b" * 64
    monkeypatch.setenv("SALAD_LTX25_RUN_V2_IMAGE", pinned)
    monkeypatch.setenv("SALAD_LTX25_RUN_STATIC_FP8_IMAGE", pinned)
    with pytest.raises(RuntimeError, match="cannot reuse"):
        runner._require_ltx_safe_a2v_worker()

    monkeypatch.setenv("SALAD_LTX25_RUN_STATIC_FP8_IMAGE", old_image)
    monkeypatch.setenv("SALAD_LTX25_PAGEABLE_A2V_IMAGE", old_image)
    deployed = {"image": old_image}

    class FakeSaladClient:
        def __init__(self, config: object) -> None:
            assert config.container_group_name == (
                "ai-video-factory-ltx25-worker-v3"
            )

        def describe_container_group(self) -> dict:
            return {"container": deployed}

    monkeypatch.setattr(runner, "SaladClient", FakeSaladClient)
    with pytest.raises(RuntimeError, match="worker image mismatch"):
        runner._require_ltx_safe_a2v_worker()

    deployed["image"] = pinned
    runner._require_ltx_safe_a2v_worker()


def test_capacity_guard_accepts_healthy_singleton(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runner.settings, "postgres_dsn", "postgres://worker/pg")
    monkeypatch.setattr(
        runner.settings, "salad_capacity_controller_postgres_dsn",
        "postgres://leader/pg",
    )
    seen: list[str] = []

    def fake_health(dsn: str) -> SimpleNamespace:
        seen.append(dsn)
        return SimpleNamespace(
            healthy=True, reason="healthy",
            status="running", controller_id="leader-1",
        )

    monkeypatch.setattr(runner, "read_capacity_controller_health", fake_health)
    runner._require_ltx_capacity_controller()
    assert seen == ["postgres://leader/pg"]
    assert "Capacity Controller healthy" in capsys.readouterr().out


def test_capacity_guard_rejects_missing_or_stale_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.settings, "postgres_dsn", "postgres://worker/pg")
    monkeypatch.setattr(
        runner.settings, "salad_capacity_controller_postgres_dsn", None,
    )
    monkeypatch.setattr(
        runner, "read_capacity_controller_health",
        lambda _dsn: SimpleNamespace(
            healthy=False, reason="capacity controller heartbeat is stale",
            status="running", controller_id="leader-1",
        ),
    )
    with pytest.raises(RuntimeError, match="heartbeat is stale.*No new LTX"):
        runner._require_ltx_capacity_controller()


def test_ltx_capacity_guard_blocks_paid_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    submitted: list[str] = []

    class NoSubmissionExecutor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ensure_input(self, *_args: object, **_kwargs: object) -> None:
            submitted.append("upload")

        def execute(self, *_args: object, **_kwargs: object) -> None:
            submitted.append("submit")

    def unhealthy() -> None:
        raise RuntimeError("Salad Capacity Controller is not healthy")

    monkeypatch.setattr(b_video, "InferenceJobExecutor", NoSubmissionExecutor)
    with pytest.raises(RuntimeError, match="Controller is not healthy"):
        b_video.generate_b2_ltx_video(
            tmp_path, plan, audio, images,
            queue=SimpleNamespace(), storage=SimpleNamespace(),
            check_capacity=unhealthy,
        )
    assert submitted == []
    assert list((tmp_path / "video" / "runs").glob("*/manifest.json"))



def test_memory_safe_image_gate_fails_before_paid_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        b_video, "InferenceJobExecutor",
        lambda **_kwargs: pytest.fail("executor must not be constructed"),
    )
    with pytest.raises(RuntimeError, match="safe worker image is not deployed"):
        b_video.generate_b2_ltx_video(
            tmp_path, plan, audio, images,
            queue=SimpleNamespace(), storage=SimpleNamespace(),
            check_a2v_worker=lambda: (
                calls.append("checked"),
                (_ for _ in ()).throw(RuntimeError(
                    "safe worker image is not deployed"
                )),
            ),
        )
    assert calls == ["checked"]


def test_terminal_oom_old_run_is_preserved_and_superseded(
    tmp_path: Path,
) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    previous = tmp_path / "video" / "runs" / "old" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old", "master_audio_sha256": "a" * 64,
        "status": "running",
        "jobs": [{"job_id": "old-a2v", "status": "pending"}],
    }), encoding="utf-8")

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            assert job_id == "old-a2v"
            return QueueJobSnapshot(
                id=job_id, status=QueueJobStatus.FAILED,
                provider_payload={
                    "attempt_count": 5,
                    "last_error": "AcceleratorError: CUDA error: out of memory",
                },
            )

    b_video._reconcile_prior_failed_video_runs(
        tmp_path, new_digest="safe-v2", audio_sha256="a" * 64, queue=Queue()
    )
    old = json.loads(previous.read_text(encoding="utf-8"))
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "safe-v2"
    assert old["previous_status"] == "running"
    assert old["jobs"][0]["job_id"] == "old-a2v"
    assert not b_video.has_pending_b2_video(tmp_path)


def test_all_ltx_jobs_are_published_before_first_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.compositor import b_video as compositor
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    plan, audio, images = _case(tmp_path, monkeypatch)
    events: list[tuple[str, str]] = []
    submitted_metadata: dict[str, dict[str, str]] = {}
    metadata_file = tmp_path / "worker-metadata.json"
    metadata_file.write_text(json.dumps({
        "generation_profile": LTX_A2V_RUN_OPTIMIZED_PROFILE,
        "negative_prompt_applied": True,
        "positive_prompt_enhanced": False,
        "quantization": "fp8_cast",
        "offload_mode": "none",
        "diffvae_backend_effective": "triton",
        "vae_backend_request": LTX_A2V_RUN_VAE_BACKEND,
        "guidance_forward_batch_size": LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
        "progressive_block_group_size": LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
        "stage1_residency": "gpu",
        "stage2_residency": "progressive_cpu",
        "stage_1_image_strength": 0.8,
        "video_cfg_scale": 1.25,
    }), encoding="utf-8")

    class FakeQueue:
        def submit(self, request: object, *, metadata: object) -> QueueJobSnapshot:
            submitted_metadata[request.job_id] = dict(metadata)
            events.append(("submit", request.job_id))
            return QueueJobSnapshot(
                id=request.job_id,
                status=QueueJobStatus.PENDING,
            )

    class FakeStorage:
        def stat(self, _key: str) -> SimpleNamespace:
            return SimpleNamespace(
                metadata={"artifact-sha256": _sha(metadata_file)}
            )

        def download(self, _key: str, destination: Path) -> None:
            shutil.copyfile(metadata_file, destination)

    class FakeExecutor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ensure_input(self, _source: Path, **_kwargs: object) -> None:
            pass

        def execute(self, request: object, **_kwargs: object) -> SimpleNamespace:
            events.append(("execute", request.job_id))
            return SimpleNamespace(replayed=False)

        def download_output(self, _response: object, destination: Path) -> None:
            destination.write_bytes(b"generated-clip")

    def fake_render(output: Path, *args: object) -> dict[str, object]:
        del args
        final = output / "video" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"composed")
        return {"file": "video/final.mp4", "sha256": _sha(final)}

    monkeypatch.setattr(b_video, "InferenceJobExecutor", FakeExecutor)
    monkeypatch.setattr(b_video, "cached_inference_response", lambda *_args: None)
    monkeypatch.setattr(b_video, "_probe_video", lambda *_args: None)
    monkeypatch.setattr(compositor, "render_b2_ltx_video", fake_render)

    result = b_video.generate_b2_ltx_video(
        tmp_path,
        plan,
        audio,
        images,
        queue=FakeQueue(),
        storage=FakeStorage(),
    )

    assert result["status"] == "completed"
    assert [kind for kind, _job_id in events[:3]] == ["submit", "submit", "submit"]
    assert [kind for kind, _job_id in events[3:]] == ["execute", "execute", "execute"]
    assert {job_id for kind, job_id in events if kind == "submit"} == {
        job_id for kind, job_id in events if kind == "execute"
    }
    assert {value["run_id"] for value in submitted_metadata.values()} == {
        result["fingerprint"]
    }
    assert all(
        "run_job_count" not in value for value in submitted_metadata.values()
    )


def test_legacy_run_metadata_manifest_is_migrated_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    _timeline, jobs, run_dir = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    manifest = b_video._new_manifest(jobs, audio, run_dir.name)
    counts = {"a2v": 2, "i2v": 1}
    for job, record in zip(jobs, manifest["jobs"], strict=True):
        record["request_sha256"] = b_video._legacy_run_metadata_fingerprint(
            job.request,
            run_id=run_dir.name,
            run_job_count=counts[job.kind],
        )

    matches, migrated = b_video._migrate_legacy_run_metadata_manifest(
        manifest, jobs, run_id=run_dir.name
    )

    assert matches is True
    assert migrated is True
    assert manifest["request_contract_migration"] == "ltx-run-metadata-postgres-v1"
    assert [record["request_sha256"] for record in manifest["jobs"]] == [
        job.request.fingerprint() for job in jobs
    ]


def test_explicit_image_reconciliation_cancels_only_unresolved_old_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    plan, audio, images = _case(tmp_path, monkeypatch)
    _timeline, _jobs, new_run = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    previous = tmp_path / "video" / "runs" / "old-images" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old-images",
        "master_audio_sha256": audio["sha256"],
        "status": "running",
        "jobs": [
            {"job_id": "old-pending", "status": "pending"},
            {"job_id": "old-succeeded", "status": "pending"},
            {"job_id": "old-failed", "status": "pending"},
        ],
    }), encoding="utf-8")
    cancelled: list[str] = []

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            status = {
                "old-pending": QueueJobStatus.PENDING,
                "old-succeeded": QueueJobStatus.SUCCEEDED,
                "old-failed": QueueJobStatus.FAILED,
            }[job_id]
            return QueueJobSnapshot(id=job_id, status=status)

        def cancel(self, job_id: str) -> None:
            assert job_id == "old-pending"
            cancelled.append(job_id)

    result = b_video.reconcile_obsolete_b2_video_runs(
        tmp_path, plan, audio, images, queue=Queue()
    )

    assert result == {
        "new_digest": new_run.name,
        "superseded_runs": 1,
        "cancelled_jobs": 1,
    }
    assert cancelled == ["old-pending"]
    saved = json.loads(previous.read_text(encoding="utf-8"))
    assert saved["status"] == "superseded"
    assert saved["superseded_by"] == new_run.name
    assert saved["superseded_reason"] == "explicit B2 video reconciliation"


def test_explicit_image_reconciliation_preserves_jobs_reused_by_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    plan, audio, images = _case(tmp_path, monkeypatch)
    _timeline, jobs, new_run = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    reusable_id = jobs[0].request.job_id
    previous = tmp_path / "video" / "runs" / "old-images" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old-images",
        "master_audio_sha256": audio["sha256"],
        "status": "running",
        "jobs": [
            {"job_id": reusable_id, "status": "pending"},
            {"job_id": "obsolete-pending", "status": "pending"},
        ],
    }), encoding="utf-8")
    cancelled: list[str] = []
    looked_up: list[str] = []

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            looked_up.append(job_id)
            assert job_id == "obsolete-pending"
            return QueueJobSnapshot(id=job_id, status=QueueJobStatus.PENDING)

        def cancel(self, job_id: str) -> None:
            cancelled.append(job_id)

    result = b_video.reconcile_obsolete_b2_video_runs(
        tmp_path, plan, audio, images, queue=Queue()
    )

    assert result == {
        "new_digest": new_run.name,
        "superseded_runs": 1,
        "cancelled_jobs": 1,
    }
    assert looked_up == ["obsolete-pending"]
    assert cancelled == ["obsolete-pending"]


def test_explicit_image_reconciliation_never_force_cancels_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    plan, audio, images = _case(tmp_path, monkeypatch)
    previous = tmp_path / "video" / "runs" / "old-active" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old-active",
        "master_audio_sha256": audio["sha256"],
        "status": "running",
        "jobs": [{"job_id": "old-running", "status": "pending"}],
    }), encoding="utf-8")

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            return QueueJobSnapshot(id=job_id, status=QueueJobStatus.RUNNING)

        def cancel(self, job_id: str) -> None:
            raise RuntimeError(f"active lease: {job_id}")

    with pytest.raises(b_video.B2VideoError, match="actively leased"):
        b_video.reconcile_obsolete_b2_video_runs(
            tmp_path, plan, audio, images, queue=Queue()
        )

    assert json.loads(previous.read_text(encoding="utf-8"))["status"] == "running"


def test_terminal_a2v_diffvae_budget_old_run_can_be_superseded(tmp_path: Path) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    previous = tmp_path / "video" / "runs" / "old-v2" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "v2", "master_audio_sha256": "a" * 64,
        "status": "running",
        "jobs": [{"job_id": "a2v-v2", "kind": "a2v", "status": "pending"}],
    }), encoding="utf-8")

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            assert job_id == "a2v-v2"
            return QueueJobSnapshot(
                id=job_id, status=QueueJobStatus.FAILED,
                provider_payload={
                    "attempt_count": 5,
                    "last_error": (
                        "ValueError: Cannot fit a DiffVAE decode tile under the memory budget: "
                        "stage4_feature_bytes=1022361600, usable_bytes=0."
                    ),
                },
            )

    b_video._reconcile_prior_failed_video_runs(
        tmp_path, new_digest="autotiling-v3", audio_sha256="a" * 64, queue=Queue()
    )
    old = json.loads(previous.read_text(encoding="utf-8"))
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "autotiling-v3"
    assert old["jobs"][0]["job_id"] == "a2v-v2"
    assert not b_video.has_pending_b2_video(tmp_path)


@pytest.mark.parametrize("status", ["running", "succeeded", "cancelled"])
def test_unresolved_old_job_never_triggers_a_second_paid_run(
    tmp_path: Path, status: str
) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    previous = tmp_path / "video" / "runs" / "old" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old", "master_audio_sha256": "a" * 64,
        "status": "running",
        "jobs": [{"job_id": "old-a2v", "status": "pending"}],
    }), encoding="utf-8")

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            return QueueJobSnapshot(id=job_id, status=QueueJobStatus(status))

    with pytest.raises(b_video.B2VideoError, match="refusing to submit"):
        b_video._reconcile_prior_failed_video_runs(
            tmp_path, new_digest="safe-v2", audio_sha256="a" * 64, queue=Queue()
        )
    assert json.loads(previous.read_text(encoding="utf-8"))["status"] == "running"


def test_non_oom_terminal_failure_requires_manual_reconciliation(tmp_path: Path) -> None:
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    previous = tmp_path / "video" / "runs" / "old" / "manifest.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({
        "fingerprint": "old", "master_audio_sha256": "a" * 64,
        "status": "running",
        "jobs": [{"job_id": "old-a2v", "status": "pending"}],
    }), encoding="utf-8")

    class Queue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            return QueueJobSnapshot(
                id=job_id, status=QueueJobStatus.FAILED,
                provider_payload={"last_error": "invalid source image"},
            )

    with pytest.raises(b_video.B2VideoError, match="non-OOM"):
        b_video._reconcile_prior_failed_video_runs(
            tmp_path, new_digest="safe-v2", audio_sha256="a" * 64, queue=Queue()
        )
    assert json.loads(previous.read_text(encoding="utf-8"))["status"] == "running"


def test_1080p_run_never_reuses_legacy_720p_ltx_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_video_factory.compositor import b_video as compositor
    from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus

    plan, audio, images = _case(tmp_path, monkeypatch)
    current_a2v_profile = b_video.LTX_A2V_RUN_OPTIMIZED_PROFILE
    selected_i2v = b_video.LTX_PRODUCTION_I2V_PROFILE
    new_fingerprint = b_video._fingerprint

    def old_v4_fingerprint(*args: object, **kwargs: object) -> str:
        kwargs["i2v_profile"] = None
        return new_fingerprint(*args, **kwargs)

    monkeypatch.setattr(
        b_video, "LTX_A2V_RUN_OPTIMIZED_PROFILE",
        LTX_A2V_STATIC_SAFE_V4_GENERATION_PROFILE,
    )
    monkeypatch.setattr(
        b_video, "LTX_PRODUCTION_I2V_PROFILE", LTX_GENERATION_PROFILE,
    )
    monkeypatch.setattr(b_video, "_fingerprint", old_v4_fingerprint)
    timeline, old_jobs, old_run = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    # Production saves only the 20-character directory name in manifest.fingerprint.
    # A full 64-character synthetic fingerprint hid the original recovery bug.
    old_digest = b_video._fingerprint(
        timeline, audio, b_video._source_assets(tmp_path, plan, images)[1],
        b_video._source_assets(tmp_path, plan, images)[0],
    )[:20]
    assert len(old_digest) == 20
    assert old_run.name == old_digest
    previous = b_video._new_manifest(old_jobs, audio, old_digest)
    assert previous["fingerprint"] == old_run.name
    old_run.mkdir(parents=True, exist_ok=True)
    old_run.joinpath("clips").mkdir()
    for job, record in zip(old_jobs[:2], previous["jobs"][:2], strict=True):
        clip = old_run / "clips" / f"{job.request.job_id}.mp4"
        clip.write_bytes(b"valid-completed-clip")
        record.update(
            status="completed", file=clip.relative_to(tmp_path).as_posix(),
            sha256=_sha(clip), duration_seconds=job.duration_seconds,
        )
        if job.kind == "a2v":
            old_metadata = old_run / "clips" / f"{job.request.job_id}.metadata.json"
            old_metadata.write_text(json.dumps({
                "generation_profile": LTX_A2V_STATIC_SAFE_V4_GENERATION_PROFILE,
                "negative_prompt_applied": True,
                "positive_prompt_enhanced": False,
            }), encoding="utf-8")
    b_video._save_json(old_run / "manifest.json", previous)
    monkeypatch.setattr(b_video, "LTX_A2V_RUN_OPTIMIZED_PROFILE", current_a2v_profile)
    monkeypatch.setattr(b_video, "LTX_PRODUCTION_I2V_PROFILE", selected_i2v)
    monkeypatch.setattr(b_video, "_fingerprint", new_fingerprint)
    monkeypatch.setattr(
        b_video, "_probe_video",
        lambda source, job: (
            source.read_bytes() == b"valid-completed-clip"
            or pytest.fail(f"invalid clip for {job.beats}")
        ),
    )
    submitted: list[str] = []

    class FakeQueue:
        def get(self, job_id: str) -> QueueJobSnapshot:
            assert job_id == old_jobs[2].request.job_id
            return QueueJobSnapshot(
                id=job_id, status=QueueJobStatus.FAILED,
                provider_payload={"last_error": "AcceleratorError: CUDA error: out of memory"},
            )

        def submit(self, request: object, *, metadata: object) -> QueueJobSnapshot:
            del metadata
            return QueueJobSnapshot(
                id=request.job_id, status=QueueJobStatus.PENDING,
            )

    new_metadata = tmp_path / "new-metadata.json"
    new_metadata.write_text(json.dumps({
        "generation_profile": current_a2v_profile,
        "negative_prompt_applied": True,
        "positive_prompt_enhanced": False,
        "quantization": "fp8_cast",
        "offload_mode": "none",
        "diffvae_backend_effective": "triton",
        "vae_backend_request": LTX_A2V_RUN_VAE_BACKEND,
        "guidance_forward_batch_size": LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
        "progressive_block_group_size": LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
        "stage1_residency": "gpu",
        "stage2_residency": "progressive_cpu",
        "stage_1_image_strength": 0.8,
        "video_cfg_scale": 1.25,
    }), encoding="utf-8")

    class FakeStorage:
        def stat(self, key: str) -> SimpleNamespace:
            del key
            return SimpleNamespace(metadata={"artifact-sha256": _sha(new_metadata)})

        def download(self, key: str, destination: Path) -> None:
            del key
            shutil.copyfile(new_metadata, destination)

    class FakeExecutor:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def ensure_input(self, source: Path, **kwargs: object) -> None:
            del source, kwargs

        def execute(self, request: object, **kwargs: object) -> SimpleNamespace:
            del kwargs
            submitted.append(request.job_id)
            return SimpleNamespace(replayed=False)

        def download_output(self, response: object, destination: Path) -> None:
            del response
            destination.write_bytes(b"valid-completed-clip")

    def fake_render(output: Path, *args: object) -> dict[str, object]:
        del args
        final = output / "video" / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"composed")
        return {"file": "video/final.mp4", "sha256": _sha(final)}

    monkeypatch.setattr(b_video, "InferenceJobExecutor", FakeExecutor)
    monkeypatch.setattr(b_video, "cached_inference_response", lambda *_args: None)
    monkeypatch.setattr(compositor, "render_b2_ltx_video", fake_render)
    result = b_video.generate_b2_ltx_video(
        tmp_path, plan, audio, images, queue=FakeQueue(), storage=FakeStorage(),
    )
    assert result["status"] == "completed"
    assert len(submitted) == 3
    assert all(job.request.job_id in submitted for job in b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )[1])
    manifest_file = tmp_path / "video" / "runs" / result["fingerprint"][:20] / "manifest.json"
    saved = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert saved["jobs"][0]["status"] == "completed"
    assert "reused_from_job_id" not in saved["jobs"][0]
    assert saved["jobs"][0]["job_id"] != old_jobs[0].request.job_id
    assert saved["jobs"][1]["job_id"] in submitted
    assert saved["jobs"][1]["job_id"] != old_jobs[1].request.job_id
    assert "reused_from_job_id" not in saved["jobs"][1]
    assert saved["jobs"][2]["job_id"] in submitted
    assert saved["jobs"][2]["job_id"] != old_jobs[2].request.job_id
    assert "reused_from_job_id" not in saved["jobs"][2]
    older = json.loads((old_run / "manifest.json").read_text(encoding="utf-8"))
    assert older["status"] == "superseded"
    assert older["jobs"][0]["status"] == "completed"


def test_pending_ltx_manifest_protects_paid_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video" / "runs" / "abc" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status": "running"}', encoding="utf-8")
    assert b_video.has_pending_b2_video(tmp_path)
    path.write_text('{"status": "completed"}', encoding="utf-8")
    assert not b_video.has_pending_b2_video(tmp_path)
    path.write_text('{"status": "superseded"}', encoding="utf-8")
    assert not b_video.has_pending_b2_video(tmp_path)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are required for an actual offline composition",
)
def test_offline_ltx_dispatch_renders_one_master_audio_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    source = tmp_path / "worker-with-embedded-audio.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=1920x1080:r=24:d=3.2",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
        "-t", "3.2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)
    metadata = tmp_path / "worker-metadata.json"
    metadata.write_text(json.dumps({
        "generation_profile": LTX_A2V_RUN_OPTIMIZED_PROFILE,
        "negative_prompt_applied": True,
        "positive_prompt_enhanced": False,
        "quantization": "fp8_cast",
        "offload_mode": "none",
        "diffvae_backend_effective": "triton",
        "vae_backend_request": LTX_A2V_RUN_VAE_BACKEND,
        "guidance_forward_batch_size": LTX_A2V_RUN_GUIDANCE_FORWARD_BATCH_SIZE,
        "progressive_block_group_size": LTX_A2V_RUN_PROGRESSIVE_BLOCK_GROUP_SIZE,
        "stage1_residency": "gpu",
        "stage2_residency": "progressive_cpu",
        "stage_1_image_strength": 0.8,
        "video_cfg_scale": 1.25,
    }), encoding="utf-8")
    submitted: list[str] = []
    input_names: list[str] = []

    class FakeStorage:
        def stat(self, _key: str) -> SimpleNamespace:
            return SimpleNamespace(metadata={"artifact-sha256": _sha(metadata)})

        def download(self, _key: str, destination: Path) -> None:
            shutil.copyfile(metadata, destination)

    class FakeQueue:
        def submit(self, request: object, *, metadata: object):
            del metadata
            from ai_video_factory.providers.job_queue import QueueJobSnapshot, QueueJobStatus
            return QueueJobSnapshot(
                id=request.job_id,
                status=QueueJobStatus.PENDING,
            )

    class FakeExecutor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ensure_input(self, _source: Path, *, key: str, **_kwargs: object) -> None:
            input_names.append(key)

        def execute(self, request: object, *, metadata: object) -> SimpleNamespace:
            del metadata
            submitted.append(request.job_id)
            return SimpleNamespace(replayed=False)

        def download_output(self, _response: object, destination: Path) -> None:
            shutil.copyfile(source, destination)

    monkeypatch.setattr(b_video, "InferenceJobExecutor", FakeExecutor)
    monkeypatch.setattr(b_video, "cached_inference_response", lambda *_args: None)
    result = b_video.generate_b2_ltx_video(
        tmp_path, plan, audio, images, queue=FakeQueue(),
        storage=FakeStorage(),
    )
    assert result["status"] == "completed"
    assert result["i2v_jobs"] == 1
    assert result["a2v_jobs"] == 2
    assert result["beat_count"] == 6
    assert result["audio_source"] == "original_fish_pcm_master_only"
    assert len(submitted) == 3
    assert len([key for key in input_names if "/audio/" in key]) == 2
    assert _sha(tmp_path / result["file"]) == result["sha256"]
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,nb_frames:format=duration",
            "-of", "json", str(tmp_path / result["file"]),
        ], capture_output=True, text=True, check=True,
    )
    details = json.loads(probe.stdout)
    assert [stream["codec_type"] for stream in details["streams"]] == [
        "video", "audio",
    ]
    assert int(details["streams"][0]["nb_frames"]) == 72
    assert float(details["format"]["duration"]) == pytest.approx(3.0, abs=0.15)
    saved = json.loads((tmp_path / result["timeline_file"]).read_text(encoding="utf-8"))
    assert saved["beats"][1]["ltx_job_id"] == saved["beats"][0]["ltx_job_id"]
    assert saved["beats"][2]["ltx_job_id"] == saved["beats"][0]["ltx_job_id"]
    assert saved["beats"][3]["ltx_job_id"] != saved["beats"][0]["ltx_job_id"]
    motion = saved["beats"][4]["motion"]
    assert motion["version"] == "media-image-subpixel-affine-v3"
    assert motion["mode"] == "ken_burns"
    assert motion["direction"] == "out"
    assert motion["origin"] == "bottom_left"
    assert motion["zoom_start"] == pytest.approx(1 + (14 / 24) * 0.02)
    assert motion["zoom_end"] == 1.0
    again = b_video.generate_b2_ltx_video(
        tmp_path, plan, audio, images, queue=FakeQueue(),
        storage=FakeStorage(),
    )
    assert again["sha256"] == result["sha256"]
    assert len(submitted) == 3


def test_fast_static_run_never_inherits_old_disk_offload_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, audio, images = _case(tmp_path, monkeypatch)
    timeline, jobs, _run_dir = b_video.build_b2_video_plan(
        tmp_path, plan, audio, images
    )
    first = next(job for job in jobs if job.kind == "a2v")
    image_sha = next(item.sha256 for item in first.request.inputs if item.name == "image")
    audio_sha = next(item.sha256 for item in first.request.inputs if item.name == "audio")
    old_job_id = b_video.ltx_a2v_application_job_id(
        segment_id=f"b2-{first.beats[0]}-{first.beats[-1]}",
        prompt=first.request.parameters["prompt"], image_sha256=image_sha,
        audio_sha256=audio_sha, seed=int(first.request.parameters["seed"]),
        width=1280, height=720, fps=b_video._FPS,
        generation_profile=LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
    )
    assert first.request.job_id != old_job_id
    avatar, assets = b_video._source_assets(tmp_path, plan, images)
    assert b_video._fingerprint(timeline, audio, assets, avatar) != b_video._fingerprint(
        timeline, audio, assets, avatar,
        profile=LTX_A2V_STATIC_SAFE_GENERATION_PROFILE,
    )

    monkeypatch.setattr(
        b_video, "_reconcile_prior_failed_video_runs",
        lambda *_args, **_kwargs: None,
    )
    assert not hasattr(b_video, "_inherit_completed_v4_clips")

    def before_paid_submission() -> None:
        raise RuntimeError("stopped before paid inference")

    with pytest.raises(RuntimeError, match="stopped before paid inference"):
        b_video.generate_b2_ltx_video(
            tmp_path, plan, audio, images,
            queue=SimpleNamespace(), storage=SimpleNamespace(),
            check_i2v_worker=before_paid_submission,
        )
