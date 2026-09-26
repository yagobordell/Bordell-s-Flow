"""Automatic static B2 preview: source-aligned STT beats, PNGs and Fish narration."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import unicodedata
import wave
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", re.ASCII)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_FPS = 30
_WIDTH = 1280
_HEIGHT = 720


class StaticPreviewError(RuntimeError):
    """A required artifact is missing or cannot be aligned without guessing."""


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise StaticPreviewError(f"Refusing linked preview output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise StaticPreviewError(f"Refusing linked temporary preview output: {temporary}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _verified_asset(output: Path, name: object, digest: object) -> Path:
    if not isinstance(name, str) or not name or not isinstance(digest, str):
        raise StaticPreviewError("Preview artifact has no valid path or SHA-256")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise StaticPreviewError(f"Unsafe preview artifact path: {name}")
    path = output / relative
    if (
        path.is_symlink() or not path.is_file()
        or not path.resolve().is_relative_to(output.resolve())
    ):
        raise StaticPreviewError(f"Missing or linked preview artifact: {path}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise StaticPreviewError(f"Preview artifact SHA-256 mismatch: {path}")
    return path


def _tokens(text: str) -> list[str]:
    words = _WORDS.findall(text)
    return [
        "".join(
            char for char in unicodedata.normalize("NFKD", word.casefold())
            if not unicodedata.combining(char)
        )
        for word in words
    ]


def build_stt_beat_timeline(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
) -> dict[str, Any]:
    """Align every B2 beat to STT words, keeping the exact WAV block offsets.

    Only punctuation, case and diacritics can vary. If STT drops, changes or
    combines spoken words, fail rather than inventing a visually plausible cut.
    """
    blocks = plan.get("blocks")
    recorded = audio.get("blocks")
    starts = audio.get("block_start_seconds")
    durations = audio.get("block_durations_seconds")
    total = audio.get("duration_seconds")
    if (
        not isinstance(blocks, list) or not blocks
        or not isinstance(recorded, list) or len(blocks) != len(recorded)
        or not isinstance(starts, list) or len(starts) != len(blocks)
        or not isinstance(durations, list) or len(durations) != len(blocks)
        or not isinstance(total, (int, float)) or not math.isfinite(total)
        or total <= 0 or audio.get("stt_complete") is not True
    ):
        raise StaticPreviewError("Preview requires complete block STT and sample-accurate audio")
    soundtrack = _verified_asset(output, audio.get("file"), audio.get("sha256"))
    with wave.open(str(soundtrack), "rb") as joined:
        if (
            joined.getframerate() != 48000 or joined.getnchannels() != 1
            or joined.getsampwidth() != 2
            or abs(joined.getnframes() / 48000 - total) > 1 / 48000
        ):
            raise StaticPreviewError("Preview WAV differs from recorded PCM sample count")

    timeline: list[dict[str, Any]] = []
    for index, (block, recording) in enumerate(zip(blocks, recorded, strict=True)):
        if block.get("block_id") != recording.get("block_id"):
            raise StaticPreviewError("B2 block order differs from the TTS block order")
        if (
            isinstance(starts[index], bool) or isinstance(durations[index], bool)
            or not isinstance(starts[index], (int, float))
            or not isinstance(durations[index], (int, float))
            or not math.isfinite(starts[index]) or not math.isfinite(durations[index])
            or starts[index] < 0 or durations[index] <= 0
            or starts[index] + durations[index] > total + 1 / 48000
        ):
            raise StaticPreviewError("Invalid sample-accurate block offset")
        if index:
            expected = starts[index - 1] + durations[index - 1] + 0.5
            if abs(starts[index] - expected) > 1 / 48000:
                raise StaticPreviewError("Narration does not contain the expected 500 ms pause")
        stt_ref = recording.get("stt")
        if not isinstance(stt_ref, dict):
            raise StaticPreviewError(f"Missing STT for block {block['block_id']}")
        stt_path = _verified_asset(output, stt_ref.get("file"), stt_ref.get("sha256"))
        transcript = json.loads(stt_path.read_text(encoding="utf-8"))
        if (
            transcript.get("source_audio_sha256") != recording.get("sha256")
            or transcript.get("model") != stt_ref.get("model")
            or not isinstance(transcript.get("words"), list)
        ):
            raise StaticPreviewError("STT source or model differs from the saved TTS block")
        aligned_words: list[dict[str, Any]] = []
        last_start = 0.0
        for word in transcript["words"]:
            if not isinstance(word, dict):
                raise StaticPreviewError("Invalid STT word record")
            text = word.get("word")
            begin, end = word.get("start"), word.get("end")
            if (
                not isinstance(text, str) or len(_tokens(text)) != 1
                or isinstance(begin, bool) or isinstance(end, bool)
                or not isinstance(begin, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(begin) or not math.isfinite(end)
                or begin < last_start or end < begin
                or end > durations[index] + 0.15
            ):
                raise StaticPreviewError("Unalignable or out-of-bounds OpenAI STT word")
            aligned_words.append({"text": _tokens(text)[0], "start": begin, "end": end})
            last_start = begin
        beats = block.get("beats")
        if not isinstance(beats, list) or not beats:
            raise StaticPreviewError("B2 block has no beats")
        expected_tokens = [_tokens(beat.get("text", "")) for beat in beats]
        actual_tokens = [word["text"] for word in aligned_words]
        if not all(expected_tokens) or [
            token for words in expected_tokens for token in words
        ] != actual_tokens:
            raise StaticPreviewError(
                f"STT words do not match B2 block {block['block_id']}; "
                "manual transcription review is required before video assembly"
            )
        cursor = 0
        for beat_index, (beat, tokens) in enumerate(zip(beats, expected_tokens, strict=True)):
            beat_id = beat.get("beat_id")
            if (
                not isinstance(beat_id, str) or not _BEAT_ID.fullmatch(beat_id)
                or not beat_id.startswith(str(block["block_id"]))
            ):
                raise StaticPreviewError("Unsafe or mismatched B2 beat identifier")
            start = (
                starts[index] if beat_index == 0
                else starts[index] + aligned_words[cursor]["start"]
            )
            timeline.append({
                "block_id": block["block_id"],
                "beat_id": beat_id,
                "visual_type": beat.get("visual_type"),
                "description": beat.get("description"),
                "start_seconds": float(start),
                "first_word_seconds": float(starts[index] + aligned_words[cursor]["start"]),
                "last_word_end_seconds": float(
                    starts[index] + aligned_words[cursor + len(tokens) - 1]["end"]
                ),
                "word_count": len(tokens),
            })
            cursor += len(tokens)

    if not timeline or abs(timeline[0]["start_seconds"]) > 1 / 48000:
        raise StaticPreviewError("The first visual beat must start with the soundtrack")
    for index, beat in enumerate(timeline):
        end = timeline[index + 1]["start_seconds"] if index + 1 < len(timeline) else total
        if end <= beat["start_seconds"]:
            raise StaticPreviewError(
                f"Nonpositive visual duration for beat {beat['beat_id']}"
            )
        beat["end_seconds"] = float(end)
        beat["duration_seconds"] = float(end - beat["start_seconds"])
        beat["start_frame"] = round(beat["start_seconds"] * _FPS)
        beat["end_frame"] = round(end * _FPS)
        if beat["end_frame"] <= beat["start_frame"]:
            raise StaticPreviewError(
                f"Beat {beat['beat_id']} is shorter than a {_FPS}-fps preview frame"
            )
    return {
        "schema_version": "b2-stt-static-timeline-v1",
        "timing_source": "openai_stt_word",
        "fps": _FPS,
        "duration_seconds": float(total),
        "audio_file": audio["file"],
        "audio_sha256": audio["sha256"],
        "beats": timeline,
    }


def _fit_image(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as source:
        if source.format != "PNG":
            raise StaticPreviewError(f"Preview source must be PNG: {path}")
        source.load()
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        white.alpha_composite(rgba)
        return ImageOps.fit(
            white.convert("RGB"), size, method=Image.Resampling.LANCZOS
        )


def _beat_frame(
    avatar: Path, media: Path | None, visual_type: str,
    destination: Path,
) -> None:
    if visual_type == "avatar":
        image = _fit_image(avatar, (_WIDTH, _HEIGHT))
    elif visual_type == "avatar_media":
        if media is None:
            raise StaticPreviewError("avatar_media beat has no generated PNG")
        image = Image.new("RGB", (_WIDTH, _HEIGHT))
        image.paste(_fit_image(avatar, (_WIDTH // 2, _HEIGHT)), (0, 0))
        image.paste(_fit_image(media, (_WIDTH // 2, _HEIGHT)), (_WIDTH // 2, 0))
    elif visual_type in {"media_image", "media_video"}:
        if media is None:
            raise StaticPreviewError(f"{visual_type} beat has no generated PNG")
        image = _fit_image(media, (_WIDTH, _HEIGHT))
    else:
        raise StaticPreviewError(f"Unknown B2 visual type: {visual_type}")
    image.save(destination, "PNG")


def generate_static_preview(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
    image_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Publish an MP4 only after all STT cuts, PNGs and the audio validate."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise StaticPreviewError("ffmpeg and ffprobe are required for the static preview")
    if image_manifest.get("status") != "completed":
        raise StaticPreviewError("Static preview requires the completed B2 image manifest")
    timeline = build_stt_beat_timeline(output, plan, audio)
    avatar_meta = plan.get("avatar")
    if not isinstance(avatar_meta, dict):
        raise StaticPreviewError("Static preview requires the saved avatar")
    avatar = _verified_asset(output, avatar_meta.get("file"), avatar_meta.get("sha256"))
    assets: dict[tuple[int, str], Path] = {}
    for item in image_manifest.get("items", []):
        key = item.get("block_id"), item.get("beat_id")
        if key in assets:
            raise StaticPreviewError("Duplicate generated PNG for one B2 beat")
        assets[key] = _verified_asset(output, item.get("file"), item.get("sha256"))
    preview = output / "preview"
    if preview.is_symlink():
        raise StaticPreviewError("Refusing linked preview directory")
    frames = preview / "frames"
    if frames.is_symlink():
        raise StaticPreviewError("Refusing linked preview frames directory")
    frames.mkdir(parents=True, exist_ok=True)
    concat = preview / "timeline.ffconcat"
    if concat.is_symlink():
        raise StaticPreviewError("Refusing linked FFmpeg manifest")
    entries = ["ffconcat version 1.0"]
    for index, beat in enumerate(timeline["beats"]):
        key = beat["block_id"], beat["beat_id"]
        frame_name = f"{index:04d}_{beat['beat_id']}.png"
        frame = frames / frame_name
        if frame.is_symlink():
            raise StaticPreviewError("Refusing linked preview frame")
        _beat_frame(avatar, assets.get(key), beat["visual_type"], frame)
        entries.append(f"file 'frames/{frame_name}'")
        entries.append(f"duration {beat['duration_seconds']:.9f}")
    entries.append(entries[-2])  # Repeated last frame preserves its last duration.
    concat.write_text("\n".join(entries) + "\n", encoding="utf-8")
    _save_json(preview / "timeline.json", timeline)

    target = preview / "static_preview.mp4"
    temporary = preview / "static_preview.part.mp4"
    if target.is_symlink() or temporary.is_symlink():
        raise StaticPreviewError("Refusing linked preview video")
    temporary.unlink(missing_ok=True)
    try:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-i", str(output / audio["file"]),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"fps={_FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{timeline['duration_seconds']:.9f}",
            "-movflags", "+faststart", str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise StaticPreviewError("FFmpeg static preview failed: " + result.stderr[-1400:])
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(temporary),
            ],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            raise StaticPreviewError("Cannot probe rendered preview: " + probe.stderr[-500:])
        result_info = json.loads(probe.stdout)
        actual_duration = float(result_info["format"]["duration"])
        stream_types = {stream["codec_type"] for stream in result_info["streams"]}
        if (
            not {"video", "audio"}.issubset(stream_types)
            or abs(actual_duration - timeline["duration_seconds"]) > 0.15
        ):
            raise StaticPreviewError("Preview duration or audio/video streams are invalid")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    with target.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "status": "completed",
        "schema_version": "b2-static-preview-v1",
        "file": target.relative_to(output).as_posix(),
        "sha256": sha,
        "duration_seconds": timeline["duration_seconds"],
        "rendered_duration_seconds": actual_duration,
        "fps": _FPS,
        "width": _WIDTH,
        "height": _HEIGHT,
        "beat_count": len(timeline["beats"]),
        "timeline_file": (preview / "timeline.json").relative_to(output).as_posix(),
        "audio_file": audio["file"],
        "stt_model": audio["stt_model"],
        "avatar_media_layout": "side_by_side_50_50",
    }
    _save_json(preview / "manifest.json", manifest)
    return manifest
