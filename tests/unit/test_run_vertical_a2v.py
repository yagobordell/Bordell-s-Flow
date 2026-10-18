from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_video_factory.providers.job_queue import QueueJobStatus

from scripts.pipeline import run_vertical_a2v as vertical


def test_vertical_dimensions_are_native_grid_9_by_16() -> None:
    assert vertical.HEIGHT * 9 == vertical.WIDTH * 16
    assert vertical.WIDTH % 64 == vertical.HEIGHT % 64 == 0


@pytest.mark.parametrize("seconds", [0.2, 5.7, 11.25, 12, 15.386, 23, 61, 135])
def test_planner_covers_every_second_without_oversized_jobs(seconds: float) -> None:
    result, forced = vertical.plan_segments(seconds, [])
    assert result[0][0] == 0.0
    assert result[-1][1] == seconds
    assert forced == max(0, len(result) - 1)
    for (start, end), next_part in zip(result, result[1:], strict=False):
        assert start < end
        assert end - start <= vertical.MAX_SEGMENT + 0.0001
        assert next_part[0] == end


def test_planner_prefers_natural_pause_and_does_not_make_tiny_tail() -> None:
    parts, forced = vertical.plan_segments(15.386, [1.0, 7.8, 14.8])
    assert forced == 0
    assert parts == [(0, 7.8), (7.8, 15.386)]
    nearly_limit, forced = vertical.plan_segments(11.30, [11.0])
    assert forced == 1
    assert min(end - start for start, end in nearly_limit) > 5


def test_nonpositive_length_is_rejected() -> None:
    with pytest.raises(ValueError):
        vertical.plan_segments(0, [])


def test_portrait_input_keeps_subject_uncropped(tmp_path: Path) -> None:
    # An original wide portrait has a bright center; fitted content remains visible.
    source = tmp_path / "image.jpeg"
    Image.new("RGB", (1280, 720), (230, 20, 20)).save(source)
    result = tmp_path / "portrait.png"
    vertical.prepare_portrait(source, result)
    with Image.open(result) as opened:
        assert opened.size == (576, 1024)
        assert opened.getpixel((288, 512))[0] > 210


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg"
)
def test_real_offline_plan_is_resumable_and_mp3_safe(tmp_path: Path) -> None:
    image = tmp_path / "avatar.jpg"
    Image.new("RGB", (600, 800), (50, 90, 130)).save(image)
    mp3 = tmp_path / "voice.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=500:duration=15.386", "-c:a", "libmp3lame", str(mp3),
    ], check=True)
    script = tmp_path / "guion.txt"
    script.write_text("Guion de la voz", encoding="utf-8")
    folder = tmp_path / "output"
    flags = [
        "--image", str(image), "--audio", str(mp3), "--script", str(script),
        "--output-dir", str(folder), "--plan-only",
    ]
    assert vertical.main(flags) == 0
    first = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert len(first["segments"]) == 2
    assert sum(x["duration"] for x in first["segments"]) == pytest.approx(15.386, abs=0.05)
    assert all(x["duration"] <= 11.26 for x in first["segments"])
    assert (folder / "work" / "reference.png").is_file()
    assert vertical.main(flags) == 0
    assert json.loads((folder / "manifest.json").read_text(encoding="utf-8")) == first
    # Correcting the optional transcript must not require a fresh GPU inference.
    script.write_text("Nuevo guion", encoding="utf-8")
    assert vertical.main(flags) == 0
    corrected = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert corrected["identity"] == first["identity"]
    assert corrected["script_sha256"] != first["script_sha256"]
    # A changed source image cannot reuse existing generated clips.
    Image.new("RGB", (600, 800), (200, 90, 130)).save(image)
    with pytest.raises(SystemExit, match="different inputs/cuts"):
        vertical.main(flags)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg"
)
def test_real_ffmpeg_portrait_composition_uses_master_audio(tmp_path: Path) -> None:
    clips = []
    for index in range(2):
        clip = tmp_path / f"clip-{index}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=576x1024:r=24:d=0.6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6",
            "-c:v", "libx264", "-c:a", "aac", "-t", "0.6", str(clip),
        ], check=True)
        clips.append(clip)
    master = tmp_path / "master.wav"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=720:duration=1.1",
        "-c:a", "pcm_s16le", str(master),
    ], check=True)
    output = tmp_path / "vertical.mp4"
    vertical.compose(clips, [0.55, 0.55], master, output, full_hd=False)
    vertical._check_video(output)
    assert vertical._duration(output) == pytest.approx(1.1, abs=0.1)



def _snapshot(status: QueueJobStatus, gpu_attempts: int) -> SimpleNamespace:
    return SimpleNamespace(
        status=status, provider_payload={"attempt_count": gpu_attempts}
    )


def test_cancelled_retry_is_explicit_and_never_reuses_an_existing_job() -> None:
    cancelled = _snapshot(QueueJobStatus.CANCELLED, 0)
    with pytest.raises(RuntimeError, match="RetryCancelled"):
        vertical.maybe_advance_cancelled(0, cancelled, retry_cancelled=False)
    assert vertical.maybe_advance_cancelled(0, cancelled, retry_cancelled=True) == 1
    assert vertical.maybe_advance_cancelled(2, cancelled, retry_cancelled=True) == 3


@pytest.mark.parametrize(
    "status",
    [QueueJobStatus.PENDING, QueueJobStatus.RUNNING, QueueJobStatus.SUCCEEDED],
)
def test_non_cancelled_jobs_are_never_requeued(status: QueueJobStatus) -> None:
    assert vertical.maybe_advance_cancelled(
        0, _snapshot(status, 0), retry_cancelled=True,
    ) == 0
    assert vertical.maybe_advance_cancelled(
        0, None, retry_cancelled=True,
    ) == 0


def test_cancelled_with_previous_gpu_work_never_auto_retries() -> None:
    with pytest.raises(RuntimeError, match="unsafe"):
        vertical.maybe_advance_cancelled(
            1, _snapshot(QueueJobStatus.CANCELLED, 1), retry_cancelled=True,
        )


def test_segment_retry_changes_only_its_transport_id(tmp_path: Path, monkeypatch) -> None:
    manifest = {
        "segments": [
            {"label": "part-001", "audio_sha256": "b" * 64, "retry_count": 0},
            {"label": "part-002", "audio_sha256": "c" * 64, "retry_count": 0,
             "video_sha256": "d" * 64},
        ],
    }
    first = manifest["segments"][0]
    other = dict(manifest["segments"][1])
    digest = "f" * 20
    kwargs = {
        "digest": digest, "prompt": "talking to the camera",
        "seed": 4242, "image_sha256": "a" * 64,
    }
    initial_segment, initial_job = vertical.segment_job_identity(
        label="part-001", attempt=0, audio_sha256=first["audio_sha256"], **kwargs,
    )
    monkeypatch.setattr(
        vertical, "_existing_job_snapshot",
        lambda job_id: (
            _snapshot(QueueJobStatus.CANCELLED, 0) if job_id == initial_job else None
        ),
    )
    path = tmp_path / "manifest.json"
    retry_id = vertical.prepare_segment_retry(
        first, manifest=manifest, manifest_path=path, retry_cancelled=True, **kwargs,
    )
    assert retry_id != initial_segment
    assert first["retry_count"] == 1
    assert manifest["segments"][1] == other
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    _, new_job = vertical.segment_job_identity(
        label="part-001", attempt=1, audio_sha256=first["audio_sha256"], **kwargs,
    )
    assert initial_job != new_job
    # Restart while the new job is pending; keep exactly the same id.
    monkeypatch.setattr(
        vertical, "_existing_job_snapshot",
        lambda _: _snapshot(QueueJobStatus.PENDING, 0),
    )
    assert vertical.prepare_segment_retry(
        first, manifest=manifest, manifest_path=path, retry_cancelled=True, **kwargs,
    ) == retry_id
    assert first["retry_count"] == 1
