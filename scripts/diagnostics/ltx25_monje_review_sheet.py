"""Create a contact sheet to review zoom, lips and head motion without modifying the MP4.

The same central crop is used only in the review image, never in the generated
video. This is a visual aid, NOT automatic proof of camera or lip-sync quality.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


def sample_times(duration: float, fps: float, *, count: int = 6) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be positive and finite")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Video fps must be positive and finite")
    if count < 2:
        raise ValueError("Review sheet needs at least two different times")
    last_frame_time = max(0.0, duration - 1.0 / fps)
    return [last_frame_time * index / (count - 1) for index in range(count)]


def probe_video(path: Path) -> tuple[float, float]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"Missing review MP4: {path}")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe is required for review sheet")
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    info = json.loads(completed.stdout)
    videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if len(videos) != 1:
        raise RuntimeError("Review MP4 must contain exactly one video stream")
    num, den = str(videos[0].get("avg_frame_rate", "0/1")).split("/", 1)
    fps = float(num) / float(den)
    return float(info["format"]["duration"]), fps


def _extract_frame(video: Path, second: float, destination: Path) -> Image.Image:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for review sheet")
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{second:.6f}", "-i", str(video),
            "-map", "0:v:0", "-frames:v", "1", str(destination),
        ],
        check=True, capture_output=True, text=True,
    )
    with Image.open(destination) as opened:
        return opened.convert("RGB")


def create_review_sheet(video: Path, destination: Path) -> dict[str, object]:
    duration, fps = probe_video(video)
    times = sample_times(duration, fps)
    width, height = 320, 180
    label_height = 30
    margin = 8
    columns = len(times)
    sheet = Image.new(
        "RGB", (margin + columns * (width + margin),
                margin + 2 * (height + label_height + margin)), "white",
    )
    draw = ImageDraw.Draw(sheet)
    with tempfile.TemporaryDirectory(prefix="ltx25-review-") as directory:
        for index, second in enumerate(times):
            frame = _extract_frame(
                video, second, Path(directory) / f"frame-{index:02d}.png"
            )
            # Constant image-space ROI allows inspection of mouth and head
            # scale; it does not stabilize, crop or replace the output MP4.
            fw, fh = frame.size
            center = frame.crop((
                round(fw * 0.25), round(fh * 0.10),
                round(fw * 0.75), round(fh * 0.90),
            ))
            for row, source in enumerate((frame, center)):
                tile = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
                x = margin + index * (width + margin)
                y = margin + row * (height + label_height + margin)
                sheet.paste(tile, (x + (width - tile.width) // 2, y + label_height))
                caption = f"{second:.2f}s " + ("FULL" if row == 0 else "FIXED CENTER ROI")
                draw.text((x, y + 5), caption, fill="black")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG")
    report: dict[str, object] = {
        "video": str(video),
        "review_sheet": str(destination),
        "sample_times_seconds": [round(value, 3) for value in times],
        "zoom_assessment": "manual_review_required",
        "lip_sync_assessment": "manual_review_with_original_audio_required",
        "output_video_modified": False,
    }
    destination.with_suffix(".json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create_review_sheet(args.video, args.output), indent=2))


if __name__ == "__main__":
    main()
