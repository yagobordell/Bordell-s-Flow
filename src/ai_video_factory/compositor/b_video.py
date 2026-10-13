"""Render verified LTX clips into the original B2/STT timeline.

LTX A2V audio is conditioning only: the final mux maps exactly one original
sample-accurate Fish narration, never the audio embedded in generated clips.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_video_factory.compositor.ken_burns import (
    build_clip_ken_burns_filter,
    motion_direction_for_index,
    motion_metadata,
)
from ai_video_factory.compositor.static_preview import (
    _beat_frame,
    _save_json,
    _verified_asset,
)
from ai_video_factory.inference.storage import sha256_file

if TYPE_CHECKING:
    from ai_video_factory.workflows.b_video import B2VideoJob

_FPS = 24
_WIDTH = 1920
_HEIGHT = 1080


class B2VideoCompositionError(RuntimeError):
    """A clip cannot be placed without losing the source timing or narration."""


def _run_ffmpeg(command: list[str], description: str) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *command],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise B2VideoCompositionError(
            f"FFmpeg {description} failed: {result.stderr[-1400:]}"
        )


def _probe_final(path: Path, duration: float, frames: int) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate,nb_frames",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise B2VideoCompositionError(
            "Cannot probe LTX final video: " + result.stderr[-600:]
        )
    data = json.loads(result.stdout)
    videos = [item for item in data["streams"] if item["codec_type"] == "video"]
    audios = [item for item in data["streams"] if item["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise B2VideoCompositionError("Final LTX video must have one video and one audio")
    video = videos[0]
    if (video["width"], video["height"]) != (_WIDTH, _HEIGHT):
        raise B2VideoCompositionError("Final LTX output is not 1920x1080")
    numerator, denominator = map(int, video.get("avg_frame_rate", "0/1").split("/"))
    if not denominator or abs(numerator / denominator - _FPS) > 0.01:
        raise B2VideoCompositionError("Final LTX video is not 24 fps")
    if video.get("nb_frames") not in (None, "N/A", str(frames)):
        raise B2VideoCompositionError("Final LTX output lost or duplicated frames")
    actual = float(data["format"]["duration"])
    if not math.isfinite(actual) or abs(actual - duration) > 0.15:
        raise B2VideoCompositionError("Final LTX duration differs from the narration")
    return actual


def _fit(width: int) -> str:
    return (
        f"scale={width}:{_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={width}:{_HEIGHT},setsar=1"
    )


def _image_records(images: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the immutable manifest records to the bytes used for composition."""
    if images.get("finishing_status") == "completed":
        final_assets = images.get("final_assets")
        if not isinstance(final_assets, list):
            raise B2VideoCompositionError(
                "Completed professional image finishing has no final_assets"
            )
        return final_assets
    return [
        *(entry for entry in images.get("items", []) if entry.get("grid") is None),
        *images.get("panel_assets", []),
    ]


def _motion_recipe(
    frames: int, image_index: int,
) -> tuple[str, dict[str, object]]:
    duration_seconds = frames / _FPS
    direction = motion_direction_for_index(image_index)
    metadata = motion_metadata(duration_seconds, direction=direction)
    filter_chain = build_clip_ken_burns_filter(
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
        duration_seconds=duration_seconds,
        direction=direction,
    )
    return (
        f"{filter_chain},trim=end_frame={frames},setpts=PTS-STARTPTS",
        metadata,
    )


def _motion_filter(beat: dict[str, Any], frames: int, image_index: int) -> str:
    filter_chain, metadata = _motion_recipe(frames, image_index)
    beat["motion"] = metadata
    return filter_chain


def render_b2_ltx_video(
    output: Path, run_dir: Path, timeline: dict[str, Any],
    jobs: list[B2VideoJob], manifest: dict[str, Any],
    images: dict[str, Any], audio: dict[str, Any],
    *, cta_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transcode beat-level visuals, concatenate, and mux the ORIGINAL PCM master."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise B2VideoCompositionError("ffmpeg and ffprobe are required for B2 LTX video")
    if run_dir.is_symlink():
        raise B2VideoCompositionError("Refusing linked LTX render directory")
    composition_started = time.perf_counter()
    beats = timeline["beats"]
    avatar = _verified_asset(
        output, manifest.get("avatar_file", "avatar.png"),
        manifest.get("avatar_sha256", sha256_file(output / "avatar.png")),
    )
    assets: dict[tuple[int, str], list[tuple[int, str, Path]]] = {}
    seen_assets: set[tuple[int, str]] = set()
    for item in _image_records(images):
        parent = item["block_id"], item["beat_id"]
        asset_id = item.get("asset_id", item["beat_id"])
        identity = item["block_id"], asset_id
        if identity in seen_assets:
            raise B2VideoCompositionError(f"Duplicate PNG asset for {identity}")
        seen_assets.add(identity)
        subshot_index = item.get("subshot_index", 1)
        if not isinstance(subshot_index, int) or subshot_index < 1:
            raise B2VideoCompositionError("Invalid B2 image subshot index")
        assets.setdefault(parent, []).append((
            subshot_index,
            asset_id,
            _verified_asset(output, item["file"], item["sha256"]),
        ))
    for records in assets.values():
        records.sort(key=lambda item: item[0])

    def primary_asset(key: tuple[int, str]) -> Path:
        records = assets.get(key)
        if not records:
            raise B2VideoCompositionError(f"Missing B2 PNG for {key}")
        return records[0][2]

    by_index: dict[int, tuple[B2VideoJob, dict[str, Any], Path]] = {}
    for job, record in zip(jobs, manifest["jobs"], strict=True):
        clip = _verified_asset(output, record.get("file"), record.get("sha256"))
        for i in range(job.first_index, job.last_index + 1):
            if i in by_index:
                raise B2VideoCompositionError("Overlapping LTX jobs in visual timeline")
            by_index[i] = job, record, clip

    segments = run_dir / "segments"
    stills = run_dir / "frames"
    if segments.is_symlink() or stills.is_symlink():
        raise B2VideoCompositionError("Refusing linked LTX render inputs")
    segments.mkdir(parents=True, exist_ok=True)
    stills.mkdir(parents=True, exist_ok=True)
    concat_entries = ["ffconcat version 1.0"]
    previous_end = 0
    total_frames = 0
    image_count = 0

    segment_prepare_started = time.perf_counter()
    for index, beat in enumerate(beats):
        first = round(beat["start_seconds"] * _FPS)
        last = round(beat["end_seconds"] * _FPS)
        if first != previous_end or last <= first:
            raise B2VideoCompositionError(
                f"Invalid 24-fps B2 cut at beat {beat['beat_id']}"
            )
        frames = last - first
        previous_end = last
        total_frames += frames
        kind = beat["visual_type"]
        key = beat["block_id"], beat["beat_id"]
        segment = segments / f"{index:04d}_{beat['beat_id']}.mp4"
        temporary = segment.with_suffix(".part.mp4")
        if segment.is_symlink() or temporary.is_symlink():
            raise B2VideoCompositionError("Refusing linked LTX beat clip")
        temporary.unlink(missing_ok=True)
        try:
            if kind == "media_image" and index not in by_index:
                records = assets.get(key)
                if not records:
                    raise B2VideoCompositionError(
                        f"Missing B2 still image for {beat['beat_id']}"
                    )
                if len(records) == 1:
                    source = records[0][2]
                    frame = stills / f"{index:04d}_{beat['beat_id']}.png"
                    if frame.is_symlink():
                        raise B2VideoCompositionError("Refusing linked B2 still frame")
                    _beat_frame(avatar, source, kind, frame)
                    command = [
                        "-loop", "1", "-framerate", str(_FPS), "-i", str(frame),
                        "-vf", _motion_filter(beat, frames, image_count),
                    ]
                    image_count += 1
                else:
                    count = len(records)
                    if frames < count:
                        raise B2VideoCompositionError(
                            f"Beat {beat['beat_id']} is too short for {count} still subshots"
                        )
                    base, remainder = divmod(frames, count)
                    inputs: list[str] = []
                    filters: list[str] = []
                    labels: list[str] = []
                    motions: list[dict[str, object]] = []
                    rendered: list[dict[str, object]] = []
                    cursor = first
                    for subindex, (_order, asset_id, source) in enumerate(
                        records, start=1
                    ):
                        subframes = base + (1 if subindex <= remainder else 0)
                        frame = stills / (
                            f"{index:04d}_{beat['beat_id']}__{subindex}.png"
                        )
                        if frame.is_symlink():
                            raise B2VideoCompositionError(
                                "Refusing linked B2 still subshot frame"
                            )
                        _beat_frame(avatar, source, kind, frame)
                        inputs.extend([
                            "-loop", "1", "-framerate", str(_FPS),
                            "-i", str(frame),
                        ])
                        recipe, metadata = _motion_recipe(
                            subframes, image_count
                        )
                        image_count += 1
                        label = f"v{subindex - 1}"
                        filters.append(f"[{subindex - 1}:v]{recipe}[{label}]")
                        labels.append(f"[{label}]")
                        motions.append({"asset_id": asset_id, **metadata})
                        rendered.append({
                            "asset_id": asset_id,
                            "start_frame": cursor,
                            "end_frame": cursor + subframes,
                        })
                        cursor += subframes
                    filters.append(
                        "".join(labels)
                        + f"concat=n={count}:v=1:a=0,format=yuv420p[v]"
                    )
                    command = [
                        *inputs,
                        "-filter_complex", ";".join(filters),
                        "-map", "[v]",
                    ]
                    beat["motion"] = {
                        "version": motions[0]["version"],
                        "mode": "ken_burns_subshots",
                        "subshots": motions,
                    }
                    beat["render_subshots"] = rendered
            else:
                item = by_index.get(index)
                if item is None:
                    raise B2VideoCompositionError(
                        f"Missing LTX output for beat {beat['beat_id']}"
                    )
                job, record, video = item
                expected_kind = (
                    "a2v" if kind in {"avatar", "avatar_media"} or beat.get("audio")
                    else "i2v"
                )
                if job.kind != expected_kind:
                    raise B2VideoCompositionError("B2 visual does not match LTX job")
                offset = max(0.0, beat["start_seconds"] - job.start_seconds)
                command = ["-ss", f"{offset:.9f}", "-i", str(video)]
                padding = (
                    f"tpad=stop_mode=clone:stop_duration=0.25,"
                    f"trim=end_frame={frames},setpts=PTS-STARTPTS"
                )
                if kind == "avatar_media":
                    media = primary_asset(key)
                    command.extend(["-loop", "1", "-i", str(media)])
                    command.extend([
                        "-filter_complex",
                        f"[0:v]fps={_FPS},{_fit(_WIDTH // 2)}[left];"
                        f"[1:v]fps={_FPS},{_fit(_WIDTH // 2)}[right];"
                        f"[left][right]hstack=inputs=2,format=yuv420p,"
                        f"{padding}[v]",
                        "-map", "[v]",
                    ])
                else:
                    command.extend([
                        "-vf",
                        f"fps={_FPS},{_fit(_WIDTH)},format=yuv420p,{padding}",
                    ])
            command.extend([
                "-an", "-frames:v", str(frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-bf", "0", "-pix_fmt", "yuv420p", "-r", str(_FPS),
                "-video_track_timescale", "12288",
                str(temporary),
            ])
            _run_ffmpeg(command, f"beat {beat['beat_id']}")
            temporary.replace(segment)
        finally:
            temporary.unlink(missing_ok=True)
        concat_entries.extend([
            f"file 'segments/{segment.name}'",
            f"duration {frames / _FPS:.9f}",
        ])
        beat["ltx_job_id"] = (
            by_index[index][0].request.job_id if index in by_index else None
        )
        beat["render_start_frame"] = first
        beat["render_end_frame"] = last

    segment_prepare_seconds = time.perf_counter() - segment_prepare_started
    if previous_end != round(timeline["duration_seconds"] * _FPS):
        raise B2VideoCompositionError("Final B2 frame count differs from narration")
    concat = run_dir / "timeline.ffconcat"
    if concat.is_symlink():
        raise B2VideoCompositionError("Refusing linked LTX concatenation manifest")
    concat.write_text("\n".join(concat_entries) + "\n", encoding="utf-8")
    soundtrack = _verified_asset(output, audio["file"], audio["sha256"])
    final = run_dir / "final.mp4"
    temporary = run_dir / "final.part.mp4"
    if final.is_symlink() or temporary.is_symlink():
        raise B2VideoCompositionError("Refusing linked final LTX video")
    temporary.unlink(missing_ok=True)
    overlay_metadata: dict[str, Any] | None = None
    try:
        command = [
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-i", str(soundtrack),
        ]
        if cta_plan is not None and cta_plan.get("ebook_cta_groups"):
            from ai_video_factory.compositor.professional_cta_qr import (
                build_professional_cta_mux_overlay,
            )

            prepared = build_professional_cta_mux_overlay(
                output, run_dir, timeline, cta_plan,
            )
            if prepared is not None:
                overlay_inputs, overlay_filter, overlay_metadata = prepared
                command.extend(overlay_inputs)
                command.extend(["-filter_complex", overlay_filter, "-map", "[v]"])
        if overlay_metadata is None:
            # Independent MP4 beat tracks can have timestamp gaps. Normalize
            # every decoded frame to a real 24-fps clock before final encoding.
            command.extend([
                "-map", "0:v:0",
                "-vf", f"setpts=N/({_FPS}*TB),fps={_FPS},format=yuv420p",
            ])
        command.extend([
            "-map", "1:a:0",
            "-frames:v", str(total_frames), "-r", str(_FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-video_track_timescale", "12288",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{timeline['duration_seconds']:.9f}",
            "-movflags", "+faststart", str(temporary),
        ])
        encoding_started = time.perf_counter()
        _run_ffmpeg(command, "B2 LTX final mux")
        final_encode_seconds = time.perf_counter() - encoding_started
        verification_started = time.perf_counter()
        actual = _probe_final(temporary, timeline["duration_seconds"], total_frames)
        final_verify_seconds = time.perf_counter() - verification_started
        temporary.replace(final)
    finally:
        temporary.unlink(missing_ok=True)
    timeline_path = run_dir / "timeline.json"
    _save_json(timeline_path, timeline)
    result = {
        "file": final.relative_to(output).as_posix(),
        "sha256": sha256_file(final),
        "duration_seconds": timeline["duration_seconds"],
        "rendered_duration_seconds": actual,
        "fps": _FPS, "width": _WIDTH, "height": _HEIGHT,
        "beat_count": len(beats),
        "i2v_jobs": sum(job.kind == "i2v" for job in jobs),
        "a2v_jobs": sum(job.kind == "a2v" for job in jobs),
        "timeline_file": timeline_path.relative_to(output).as_posix(),
        "audio_file": audio["file"],
        # Describe the verified master without assuming Fish: professional
        # narration may have been synthesized by Gemini, while legacy runs
        # may use a different voice backend.
        "audio_source": "verified_original_pcm_master_only",
        "audio_provider": audio.get("provider") or "unknown",
        "voice_source": audio.get("voice_source") or "unknown",
        "avatar_media_layout": "side_by_side_50_50",
        "media_image_motion": {"animated_beats": image_count},
        "composition_timings_seconds": {
            "segment_preparation": round(segment_prepare_seconds, 3),
            "final_encoding": round(final_encode_seconds, 3),
            "final_verification": round(final_verify_seconds, 3),
            "composition_total": round(time.perf_counter() - composition_started, 3),
        },
    }
    if overlay_metadata is not None:
        result["professional_ebook_qr"] = overlay_metadata
    return result
