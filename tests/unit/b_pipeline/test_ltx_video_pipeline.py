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
    LTX_A2V_STATIC_REFERENCE_GENERATION_PROFILE,
)
from ai_video_factory.workers.ltx25.model import LTX_VIDEO_TASK
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
        LTX_A2V_STATIC_REFERENCE_GENERATION_PROFILE
    )
    assert b_video.AVATAR_NEGATIVE_PROMPT == (
        "camera zoom, zoom in, push in, dolly in, camera drift, camera shake, "
        "reframing, crop change, handheld camera, unintended camera movement"
    )
    assert len(first.request.inputs) == 2
    assert {item.name for item in first.request.inputs} == {"image", "audio"}
    assert video.request.task == LTX_VIDEO_TASK
    assert video.request.job_id.startswith("b2-1D-")
    assert video.request.parameters["prompt"] == "A bird takes off."
    assert video.request.parameters["num_frames"] == 17
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
    result = asyncio.run(
        runner._video_if_ready(tmp_path, report, plan=plan, images=images)
    )
    assert result == {"status": "completed", "file": "video/final.mp4"}
    assert len(calls) == 1
    assert calls[0][:4] == (tmp_path, plan, audio, images)
    assert calls[0][4]["storage"] == "storage"
    assert closed == [True]


def test_pending_ltx_manifest_protects_paid_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video" / "runs" / "abc" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status": "running"}', encoding="utf-8")
    assert b_video.has_pending_b2_video(tmp_path)
    path.write_text('{"status": "completed"}', encoding="utf-8")
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
        "-f", "lavfi", "-i", "color=c=red:s=1280x720:r=24:d=3.2",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
        "-t", "3.2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)
    metadata = tmp_path / "worker-metadata.json"
    metadata.write_text(json.dumps({
        "generation_profile": LTX_A2V_STATIC_REFERENCE_GENERATION_PROFILE,
        "negative_prompt_applied": True,
        "positive_prompt_enhanced": False,
    }), encoding="utf-8")
    submitted: list[str] = []
    input_names: list[str] = []

    class FakeStorage:
        def stat(self, _key: str) -> SimpleNamespace:
            return SimpleNamespace(metadata={"artifact-sha256": _sha(metadata)})

        def download(self, _key: str, destination: Path) -> None:
            shutil.copyfile(metadata, destination)

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
    result = b_video.generate_b2_ltx_video(
        tmp_path, plan, audio, images, queue=SimpleNamespace(),
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
    assert saved["beats"][4]["motion"]["version"] == "media-image-zoompan-v1"
    again = b_video.generate_b2_ltx_video(
        tmp_path, plan, audio, images, queue=SimpleNamespace(),
        storage=FakeStorage(),
    )
    assert again["sha256"] == result["sha256"]
    assert len(submitted) == 3
