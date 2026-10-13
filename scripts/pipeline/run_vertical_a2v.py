"""Standalone, resumable 9:16 talking-avatar generator via the existing Salad A2V worker.

Does not touch B1/B2, their checkpoints, or Salad container capacity ownership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[2]
SUBMIT = ROOT / "scripts" / "smoke" / "submit_ltx25_a2v_smoke.py"
HEALTH = ROOT / "scripts" / "salad" / "run_salad_capacity_controller.py"
WIDTH, HEIGHT, FPS = 576, 1024, 24  # native 9:16, both divisible by LTX's 64px grid
SAMPLE_RATE = 24000
MAX_SEGMENT = 11.25  # reference A2V rejects decoded speech longer than 12 seconds
TARGET_SEGMENT = 9.0
DEFAULT_PROMPT = (
    "A medium close-up talking head of the person in the reference image, "
    "speaking naturally toward the camera in synchronization with the provided speech audio. "
    "Preserve facial identity, clothing and background. Natural blinking, restrained head "
    "movement, stable framing, realistic synchronized lip and jaw movement, no scene changes."
)


def _run(argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, capture_output=capture)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe(path: Path) -> dict:
    return json.loads(
        _run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
            str(path),
        ], capture=True).stdout
    )


def _duration(path: Path) -> float:
    value = float(_probe(path)["format"]["duration"])
    if not 0 < value < 24 * 3600:
        raise ValueError(f"Invalid or excessively long audio/video duration: {path}")
    return value


def _silence_midpoints(audio: Path) -> list[float]:
    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
        "-af", "silencedetect=noise=-35dB:d=0.18", "-f", "null", "-",
    ]
    # ffmpeg emits silence diagnostics on stderr while successfully writing no media.
    output = _run(command, capture=True).stderr
    starts: list[float] = []
    points: list[float] = []
    for line in output.splitlines():
        beginning = re.search(r"silence_start:\s*([0-9.]+)", line)
        ending = re.search(r"silence_end:\s*([0-9.]+)", line)
        if beginning:
            starts.append(float(beginning.group(1)))
        if ending and starts:
            points.append((starts.pop(0) + float(ending.group(1))) / 2)
    return points


def plan_segments(duration: float, pauses: list[float]) -> tuple[list[tuple[float, float]], int]:
    """Find natural cuts when possible, with conservative deterministic fallback cuts."""
    if duration <= 0:
        raise ValueError("Audio must have positive duration")
    parts: list[tuple[float, float]] = []
    start = 0.0
    forced = 0
    while duration - start > MAX_SEGMENT:
        remaining = duration - start
        # Avoid very short tail chunks when splitting an audio just above the limit.
        goal = start + (
            min(TARGET_SEGMENT, remaining / 2)
            if remaining < 2 * MAX_SEGMENT else TARGET_SEGMENT
        )
        eligible = [
            t for t in pauses
            if start + 1.5 <= t <= start + MAX_SEGMENT
            and duration - t >= 1.5 and abs(t - goal) <= 2.5
        ]
        if eligible:
            cut = min(eligible, key=lambda t: (abs(t - goal), t))
        else:
            cut = goal
            forced += 1
        if cut <= start or cut - start > MAX_SEGMENT:
            raise ValueError("Invalid audio segmentation boundary")
        parts.append((round(start, 6), round(cut, 6)))
        start = cut
    parts.append((round(start, 6), round(duration, 6)))
    if any(end <= begin or end - begin > MAX_SEGMENT + 0.0001 for begin, end in parts):
        raise ValueError("Audio segmentation exceeded safe reference A2V duration")
    return parts, forced


def prepare_portrait(source: Path, output: Path) -> None:
    """Letterbox with a blurred background instead of cropping a source person's face."""
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    background = ImageOps.fit(image, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(35))
    contained = ImageOps.contain(image, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
    background.paste(contained, ((WIDTH - contained.width) // 2, (HEIGHT - contained.height) // 2))
    output.parent.mkdir(parents=True, exist_ok=True)
    background.save(output, "PNG")


def _write_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _check_video(path: Path) -> None:
    probe = _probe(path)
    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    audios = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise RuntimeError(f"A2V clip lacks one video and audio track: {path}")
    if (int(videos[0]["width"]), int(videos[0]["height"])) != (WIDTH, HEIGHT):
        raise RuntimeError(f"A2V worker did not deliver native 9:16 geometry: {path}")
    numerator, denominator = map(int, videos[0]["r_frame_rate"].split("/"))
    if denominator <= 0 or abs(numerator / denominator - FPS) > 0.01:
        raise RuntimeError(f"A2V worker did not deliver {FPS} fps: {path}")


def compose(
    clips: list[Path], lengths: list[float], audio: Path, destination: Path,
    *, full_hd: bool,
) -> None:
    """Use the original normalized audio as the master track: no speech is dropped at cuts."""
    size = (1080, 1920) if full_hd else (WIDTH, HEIGHT)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd.extend(["-i", str(clip)])
    cmd.extend(["-i", str(audio)])
    filters = []
    for index, length in enumerate(lengths):
        filters.append(
            f"[{index}:v]trim=duration={length:.6f},setpts=PTS-STARTPTS,"
            f"scale={size[0]}:{size[1]}:flags=lanczos,setsar=1[v{index}]"
        )
    video_inputs = "".join(f"[v{i}]" for i in range(len(clips)))
    filters.append(f"{video_inputs}concat=n={len(clips)}:v=1:a=0[v]")
    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", f"{len(clips)}:a:0", "-t", f"{sum(lengths):.6f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(destination),
    ])
    _run(cmd)
    result = _probe(destination)
    stream = next(x for x in result["streams"] if x["codec_type"] == "video")
    if (int(stream["width"]), int(stream["height"])) != size:
        raise RuntimeError("Final video is not the requested portrait resolution")
    if abs(float(result["format"]["duration"]) - sum(lengths)) > 0.15:
        raise RuntimeError("Final video duration differs from the original speech")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-command 9:16 LTX A2V from JPEG/PNG and MP3/WAV"
    )
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument(
        "--script", type=Path, help="Optional TXT for traceability (not sent to LTX)"
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument(
        "--run-tag", default="default", help="Change explicitly to start a new paid attempt"
    )
    parser.add_argument(
        "--native-output", action="store_true",
        help="Deliver 576x1024 instead of upscaled 1080x1920",
    )
    parser.add_argument(
        "--plan-only", action="store_true",
        help="Prepare and inspect clips without Salad jobs",
    )
    parser.add_argument("--skip-controller-check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required on PATH")
    for source in (args.image, args.audio):
        if not source.is_file() or source.stat().st_size == 0:
            raise SystemExit(f"Missing or empty input: {source}")
    if args.script:
        if not args.script.is_file() or not args.script.read_text(encoding="utf-8-sig").strip():
            raise SystemExit("--script must be a nonempty UTF-8 TXT")
    if args.seed < 0 or not args.prompt.strip() or not args.run_tag.strip():
        raise SystemExit("seed must be nonnegative; prompt and run-tag must not be empty")
    if (WIDTH, HEIGHT) != (576, 1024) or WIDTH % 64 or HEIGHT % 64:
        raise RuntimeError("Native vertical LTX grid contract has changed")

    identity = {
        "image_sha256": _sha(args.image), "audio_sha256": _sha(args.audio),
        "prompt": args.prompt.strip(), "seed": args.seed, "run_tag": args.run_tag,
        "profile": "reference", "width": WIDTH, "height": HEIGHT, "fps": FPS,
        "split_policy": "silence-35db-0.18s-max11.25-target9-v1",
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    destination = args.output_dir or (
        ROOT / "data" / "output" / "vertical-a2v" / f"{args.audio.stem}-{digest[:10]}"
    )
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    work = destination / "work"
    work.mkdir(exist_ok=True)
    master = work / "master.wav"
    reference = work / "reference.png"
    print("Preparing audio and 9:16 reference image...", flush=True)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(args.audio),
        "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(master),
    ])
    duration = _duration(master)
    pauses = _silence_midpoints(master)
    boundaries, forced = plan_segments(duration, pauses)
    prepare_portrait(args.image, reference)
    segments = []
    for index, (start, end) in enumerate(boundaries, 1):
        label = f"part-{index:03d}"
        segment_audio = work / f"{label}.wav"
        first = round(start * SAMPLE_RATE)
        last = (
            round(end * SAMPLE_RATE) if index < len(boundaries)
            else round(duration * SAMPLE_RATE)
        )
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(master),
            "-af", f"atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS",
            "-c:a", "pcm_s16le", str(segment_audio),
        ])
        actual = _duration(segment_audio)
        if actual > MAX_SEGMENT + 0.01:
            raise RuntimeError(f"{label} exceeds A2V safe length: {actual:.3f} s")
        segments.append({
            "label": label, "start": start, "end": end,
            "duration": actual, "audio_sha256": _sha(segment_audio), "video_sha256": None,
        })
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("identity") != identity or [
            (x["label"], x["audio_sha256"]) for x in existing.get("segments", [])
        ] != [(x["label"], x["audio_sha256"]) for x in segments]:
            raise SystemExit(
                "Output folder belongs to different inputs/cuts. Choose a new --output-dir."
            )
        for old, new in zip(existing["segments"], segments, strict=True):
            new["video_sha256"] = old.get("video_sha256")
    manifest = {
        "schema_version": 1, "identity": identity, "request_id": digest,
        "script_sha256": _sha(args.script) if args.script else None,
        "delivery": "native" if args.native_output else "1080x1920",
        "segments": segments,
    }
    _write_json(manifest_path, manifest)
    print(
        f"Audio: {duration:.3f} s | {len(segments)} clips | non-silence cuts: {forced}",
        flush=True,
    )
    for item in segments:
        print(f"  {item['label']}: {item['duration']:.3f} s", flush=True)
    if args.plan_only:
        print(f"Plan ready (no Salad cost): {manifest_path}", flush=True)
        return 0

    if not args.skip_controller_check:
        _run([sys.executable, str(HEALTH), "--check-health"])
    clips: list[Path] = []
    for index, item in enumerate(segments, 1):
        label = item["label"]
        folder = destination / label
        folder.mkdir(exist_ok=True)
        clip = folder / "avatar_segment.mp4"
        expected = item.get("video_sha256")
        if expected and clip.is_file() and _sha(clip) == expected:
            _check_video(clip)
            print(f"[{index}/{len(segments)}] Reusing verified {label}", flush=True)
        else:
            print(f"[{index}/{len(segments)}] Submitting A2V {label}...", flush=True)
            _run([
                sys.executable, str(SUBMIT),
                "--avatar-image", str(reference),
                "--audio", str(work / f"{label}.wav"),
                "--prompt", args.prompt.strip(),
                "--profile", "reference", "--segment-id", f"vertical-{digest[:12]}-{label}",
                "--width", str(WIDTH), "--height", str(HEIGHT), "--fps", str(FPS),
                "--seed", str(args.seed), "--pending-timeout-seconds", "7200",
                "--output-dir", str(folder),
            ])
            _check_video(clip)
            item["video_sha256"] = _sha(clip)
            _write_json(manifest_path, manifest)
        clips.append(clip)
    final = destination / "video_final_9x16.mp4"
    print("Composing original audio with portrait clips...", flush=True)
    compose(clips, [x["duration"] for x in segments], master, final, full_hd=not args.native_output)
    print(f"Video: {final}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
