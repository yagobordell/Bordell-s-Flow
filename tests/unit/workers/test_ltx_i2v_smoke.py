"""Offline I2V smoke-contract checks without paid inference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from scripts.smoke.ltx25_i2v_smoke import prepare, validate


def test_prepare_one_production_i2v_shot(tmp_path: Path) -> None:
    image = tmp_path / "source.png"
    Image.new("RGB", (1280, 736), (40, 60, 80)).save(image)
    output = tmp_path / "smoke"
    prepare(image, "The leaves gently sway.", 2.0, output)
    inputs = output / "inputs"
    keyframes = json.loads((inputs / "storyboard_keyframes.json").read_text())
    prompts = json.loads((inputs / "video_prompts.json").read_text())
    timings = json.loads((inputs / "shot_timings.json").read_text())
    assert keyframes[0]["uri"] == "keyframe.png"
    assert prompts[0]["prompt"] == "The leaves gently sway."
    assert timings[0]["start_seconds"] == 0.0
    assert timings[0]["end_seconds"] == 2.0
    assert (inputs / "keyframe.png").is_file()


def test_prepare_refuses_small_nonproduction_png(tmp_path: Path) -> None:
    image = tmp_path / "small.png"
    Image.new("RGB", (640, 360)).save(image)
    with pytest.raises(ValueError, match="at least 1280x720"):
        prepare(image, "A tree sways.", 2.0, tmp_path / "out")


def test_validate_refuses_cached_replay_without_postgres_submission(
    tmp_path: Path,
) -> None:
    video = tmp_path / "output" / "video_clips" / "shot_001.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-video; rejected before ffprobe")
    manifest = {
        "transport_route": "postgres:ltx25_i2v",
        "jobs": [
            {
                "application_job_id": "fake-job",
                "transport_status": "succeeded",
                "submission_count": 0,
            }
        ],
    }
    path = tmp_path / "output" / "video_generation_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="replayed cached R2"):
        validate(video, 2.0, tmp_path)
