"""Prepare and validate one real, silent LTX-2.5 I2V smoke using Phase 8's production workflow."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from ai_video_factory.domain import ShotTiming, StoryboardKeyframe, VideoPrompt
from ai_video_factory.inference.storage import sha256_file
from ai_video_factory.workers.ltx25.model import ltx_num_frames_for_duration


def prepare(image: Path, prompt: str, duration: float, output_dir: Path) -> None:
    if not image.is_file():
        raise ValueError(f"I2V input image is missing: {image}")
    if not prompt.strip():
        raise ValueError("I2V smoke requires a non-empty motion prompt")
    if not math.isfinite(duration) or not 0.5 <= duration <= 12:
        raise ValueError("I2V smoke duration must be between 0.5 and 12 seconds")
    with Image.open(image) as opened:
        if opened.format != "PNG":
            raise ValueError("I2V smoke requires an original PNG; convert source before testing")
        width, height = opened.size
        opened.verify()
    if (width, height) != (1280, 736) and (
        width < 1280 or height < 720 or width * 9 != height * 16
    ):
        raise ValueError(
            "I2V input must be 16:9 with at least 1280x720 or Qwen 1280x736; "
            f"found {width}x{height}"
        )
    inputs = output_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    destination = inputs / "keyframe.png"
    if destination.resolve() != image.resolve():
        shutil.copyfile(image, destination)
    keyframe = StoryboardKeyframe(shot_id=1, uri="keyframe.png")
    video_prompt = VideoPrompt(shot_id=1, prompt=prompt.strip())
    timing = ShotTiming(shot_id=1, start_seconds=0.0, end_seconds=duration)
    for name, value in (
        ("storyboard_keyframes.json", keyframe),
        ("video_prompts.json", video_prompt),
        ("shot_timings.json", timing),
    ):
        (inputs / name).write_text(
            json.dumps([value.model_dump(mode="json")], indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"I2V plan ready: image_sha256={sha256_file(destination)} "
        f"duration={duration:g}s frames={ltx_num_frames_for_duration(duration, fps=24)} "
        f"inputs={inputs}"
    )


def validate(video: Path, duration: float, output_dir: Path) -> None:
    if not video.is_file() or video.stat().st_size == 0:
        raise RuntimeError(f"I2V generated MP4 is missing or empty: {video}")
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise RuntimeError("ffprobe and ffmpeg are required for I2V video validation")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        capture_output=True,
        text=True,
        check=True,
    )
    probe = json.loads(result.stdout)
    streams = probe.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or audios:
        raise RuntimeError("I2V output must contain exactly one video stream and NO audio")
    stream = videos[0]
    if stream.get("codec_name") != "h264":
        raise RuntimeError("I2V output video codec is not H.264")
    if (int(stream["width"]), int(stream["height"])) != (1280, 720):
        raise RuntimeError("I2V output geometry is not 1280x720")
    numerator, denominator = str(stream.get("avg_frame_rate") or "0/1").split("/", 1)
    fps = float(numerator) / float(denominator)
    if abs(fps - 24.0) > 1e-6:
        raise RuntimeError(f"I2V output FPS must be 24; found {fps}")
    expected_duration = ltx_num_frames_for_duration(duration, fps=24) / 24
    actual_duration = float((probe.get("format") or {}).get("duration") or 0)
    if not math.isfinite(actual_duration) or abs(actual_duration - expected_duration) > 0.1:
        raise RuntimeError(
            f"I2V duration mismatch: expected {expected_duration:.3f}s, "
            f"actual {actual_duration:.3f}s"
        )
    decoded = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(video), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if decoded.returncode:
        raise RuntimeError("I2V MP4 failed complete decode: " + decoded.stderr[-1000:])
    report = {
        "status": "technically_valid_visual_review_pending",
        "task": "video.ltx25.generate",
        "video": str(video.resolve()),
        "video_sha256": sha256_file(video),
        "size_bytes": video.stat().st_size,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "audio_streams": 0,
        "requested_duration_seconds": duration,
        "actual_duration_seconds": actual_duration,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "i2v-smoke-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--image", type=Path, required=True)
    setup.add_argument("--prompt", required=True)
    setup.add_argument("--duration-seconds", type=float, default=2.0)
    setup.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("validate")
    verify.add_argument("--video", type=Path, required=True)
    verify.add_argument("--duration-seconds", type=float, default=2.0)
    verify.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.image, args.prompt, args.duration_seconds, args.output_dir)
    else:
        validate(args.video, args.duration_seconds, args.output_dir)


if __name__ == "__main__":
    main()
