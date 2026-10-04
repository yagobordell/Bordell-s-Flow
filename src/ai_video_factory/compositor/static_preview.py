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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ai_video_factory.compositor.ken_burns import MOTION_VERSION as _MOTION_VERSION
from ai_video_factory.compositor.ken_burns import (
    build_timeline_ken_burns_filter,
    motion_direction_for_index,
    motion_metadata,
)
from ai_video_factory.providers.image_grids import BLACK_PANEL_SOURCE

_BEAT_ID = re.compile(r"[1-9][0-9]*[A-Z]+", re.ASCII)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_FPS = 30
_WIDTH = 1280
_HEIGHT = 720
_NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    "million", "billion",
})
_NUMBER_UNITS = frozenset({"cent", "cents", "dollar", "dollars", "percent", "percentage"})


class StaticPreviewError(RuntimeError):
    """A required artifact is missing or cannot be aligned without guessing."""


class SttAlignmentError(StaticPreviewError):
    """A transcript's words differ from its corresponding B2 block."""

    def __init__(self, block_id: int, message: str) -> None:
        self.block_id = block_id
        super().__init__(message)


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


def _is_number_token(token: str) -> bool:
    return token.isdecimal() or token in _NUMBER_WORDS


def _is_number_phrase_token(token: str) -> bool:
    """Return whether a token belongs to a spoken number/currency phrase."""
    return _is_number_token(token) or token in _NUMBER_UNITS


def _edit_distance_at_most_two(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 2:
        return False
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        if min(current) > 2:
            return False
        previous = current
    return previous[-1] <= 2


def _align_stt_tokens(
    expected: list[str], actual: list[str], *, block_id: int,
) -> list[int | None]:
    """Align B2 words to STT words, ignoring numbers and tiny spelling errors."""
    canonical_expected = ["<number>" if _is_number_token(token) else token for token in expected]
    canonical_actual = ["<number>" if _is_number_token(token) else token for token in actual]
    mapping: list[int | None] = [None] * len(expected)
    matcher = SequenceMatcher(a=canonical_expected, b=canonical_actual, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        expected_part = expected[i1:i2]
        actual_part = actual[j1:j2]
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset] = j1 + offset
            continue

        number_related = any(_is_number_token(token) for token in expected_part + actual_part)
        numeric_replacement = tag == "replace" and number_related
        numeric_only_change = (
            tag in {"delete", "insert"}
            and bool(expected_part or actual_part)
            and all(_is_number_token(token) for token in expected_part + actual_part)
        )
        short_spelling_change = (
            tag == "replace"
            and _edit_distance_at_most_two(
                " ".join(expected_part), " ".join(actual_part)
            )
        )
        short_omission_or_insertion = (
            tag in {"delete", "insert"}
            and sum(len(token) for token in expected_part + actual_part) <= 2
        )
        adjacent_number_unit = (
            tag == "insert"
            and j1 > 0
            and _is_number_token(actual[j1 - 1])
            and all(token in _NUMBER_UNITS for token in actual_part)
        ) or (
            tag == "delete"
            and i1 > 0
            and _is_number_token(expected[i1 - 1])
            and all(token in _NUMBER_UNITS for token in expected_part)
        )
        numeric_phrase_change = (
            tag in {"replace", "delete", "insert"}
            and bool(expected_part or actual_part)
            and all(
                _is_number_phrase_token(token)
                for token in expected_part + actual_part
            )
            and (
                any(_is_number_token(token) for token in expected_part + actual_part)
                or (i1 > 0 and _is_number_token(expected[i1 - 1]))
                or (j1 > 0 and _is_number_token(actual[j1 - 1]))
            )
        )
        if not (numeric_replacement or numeric_only_change or numeric_phrase_change
                or short_spelling_change or short_omission_or_insertion
                or adjacent_number_unit):
            expected_text = " ".join(expected_part) or "<none>"
            actual_text = " ".join(actual_part) or "<none>"
            raise SttAlignmentError(
                block_id,
                f"STT words do not match B2 block {block_id}; "
                f"token {i1 + 1}: expected {expected_text!r}, got {actual_text!r} "
                f"(expected {len(expected)} words, received {len(actual)}). "
                "Manual transcription review is required before video assembly."
            )

        if tag == "replace":
            expected_numbers = [i for i in range(i1, i2) if _is_number_token(expected[i])]
            actual_numbers = [j for j in range(j1, j2) if _is_number_token(actual[j])]
            paired_expected: set[int] = set()
            paired_actual: set[int] = set()
            for expected_index, actual_index in zip(expected_numbers, actual_numbers, strict=False):
                mapping[expected_index] = actual_index
                paired_expected.add(expected_index)
                paired_actual.add(actual_index)
            remaining_expected = [i for i in range(i1, i2) if i not in paired_expected]
            remaining_actual = [j for j in range(j1, j2) if j not in paired_actual]
            if len(remaining_actual) == 1 and remaining_expected:
                for expected_index in remaining_expected:
                    mapping[expected_index] = remaining_actual[0]
                remaining_expected = []
            for expected_index, actual_index in zip(
                remaining_expected, remaining_actual, strict=False
            ):
                mapping[expected_index] = actual_index
        elif tag == "delete":
            if (
                j1 > 0
                and _is_number_token(actual[j1 - 1])
                and expected_part
                and all(_is_number_phrase_token(token) for token in expected_part)
            ):
                # Whisper may collapse "forty-seven dollars" into one "47"
                # token and omit the currency unit. Keep all expected words
                # timed against that single numeric STT token.
                anchor = j1 - 1
            else:
                anchor = j1 if j1 < len(actual) else (j1 - 1 if j1 else None)
            for expected_index in range(i1, i2):
                mapping[expected_index] = anchor

    return mapping


def build_stt_beat_timeline(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
) -> dict[str, Any]:
    """Align every B2 beat to STT words, keeping the exact WAV block offsets.

    Numeric spelling and small transcription differences are normalized while
    word timing stays tied to STT records. Larger text differences still fail.
    """
    blocks = plan.get("blocks")
    recorded = audio.get("blocks")
    starts = audio.get("block_start_seconds")
    durations = audio.get("block_durations_seconds")
    total = audio.get("duration_seconds")
    if (
        not isinstance(blocks, list) or not blocks
        or not isinstance(recorded, list) or not recorded
        or not isinstance(starts, list) or len(starts) != len(recorded)
        or not isinstance(durations, list) or len(durations) != len(recorded)
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

    # Professional speech may merge contiguous short B2 blocks. The original
    # source blocks/beat IDs remain authoritative for every visual and LTX job.
    # Older Gemini and all Casero artifacts have no grouping manifest.
    audio_groups = audio.get("block_groups")
    source_groups: list[list[dict[str, Any]]] = []
    if audio_groups is None:
        if len(blocks) != len(recorded):
            raise StaticPreviewError("B2 block count differs from the TTS block count")
        source_groups = [[block] for block in blocks]
    else:
        if not isinstance(audio_groups, list) or len(audio_groups) != len(recorded):
            raise StaticPreviewError("Invalid professional audio block grouping")
        cursor = 0
        for specification, recording in zip(audio_groups, recorded, strict=True):
            if not isinstance(specification, dict) or not isinstance(recording, dict):
                raise StaticPreviewError("Invalid professional audio group record")
            ids = specification.get("source_block_ids")
            if not isinstance(ids, list) or not ids or any(
                not isinstance(identifier, int) or isinstance(identifier, bool)
                for identifier in ids
            ):
                raise StaticPreviewError("Invalid professional audio source block IDs")
            members = blocks[cursor:cursor + len(ids)]
            if (
                len(members) != len(ids)
                or [member.get("block_id") for member in members] != ids
                or specification.get("block_id") != ids[0]
                or recording.get("block_id") != ids[0]
                or recording.get("source_block_ids", ids) != ids
                or specification.get("beat_ids") != [
                    beat.get("beat_id") for member in members for beat in member.get("beats", [])
                ]
            ):
                raise StaticPreviewError("Professional audio groups differ from original B2 order")
            source_groups.append(members)
            cursor += len(ids)
        if cursor != len(blocks):
            raise StaticPreviewError("Professional audio grouping does not cover every B2 block")

    timeline: list[dict[str, Any]] = []
    for index, (source_group, recording) in enumerate(
        zip(source_groups, recorded, strict=True)
    ):
        block = source_group[0]
        if not isinstance(recording, dict) or block.get("block_id") != recording.get("block_id"):
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
            word_tokens = _tokens(text) if isinstance(text, str) else []
            begin, end = word.get("start"), word.get("end")
            if (
                not isinstance(text, str) or not word_tokens
                or isinstance(begin, bool) or isinstance(end, bool)
                or not isinstance(begin, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(begin) or not math.isfinite(end)
                or begin < last_start or end < begin
                or end > durations[index] + 0.15
            ):
                raise StaticPreviewError("Unalignable or out-of-bounds OpenAI STT word")
            # Whisper sometimes emits contractions as one timed record even
            # though the text tokenizer splits them into multiple tokens.
            aligned_words.extend(
                {"text": token, "start": begin, "end": end}
                for token in word_tokens
            )
            last_start = begin
        beats = [
            (source_block, beat)
            for source_block in source_group for beat in source_block["beats"]
        ]
        if not beats:
            raise StaticPreviewError("B2 voice group has no beats")
        expected_tokens = [_tokens(beat.get("text", "")) for _, beat in beats]
        flat_expected = [token for words in expected_tokens for token in words]
        if not all(expected_tokens):
            raise StaticPreviewError(
                f"B2 block {block['block_id']} contains a beat with no alignable words"
            )
        word_alignment = _align_stt_tokens(
            flat_expected,
            [word["text"] for word in aligned_words],
            block_id=block["block_id"],
        )
        cursor = 0
        for beat_index, ((source_block, beat), tokens) in enumerate(
            zip(beats, expected_tokens, strict=True)
        ):
            beat_id = beat.get("beat_id")
            if (
                not isinstance(beat_id, str) or not _BEAT_ID.fullmatch(beat_id)
                or not beat_id.startswith(str(source_block["block_id"]))
            ):
                raise StaticPreviewError("Unsafe or mismatched B2 beat identifier")
            mapped_words = [
                word_alignment[position]
                for position in range(cursor, cursor + len(tokens))
                if word_alignment[position] is not None
            ]
            if not mapped_words:
                raise StaticPreviewError(
                    f"No STT timing could be assigned to B2 beat {beat_id}"
                )
            first_word_index = min(mapped_words)
            last_word_index = max(mapped_words)
            start = starts[index] if beat_index == 0 else (
                starts[index] + aligned_words[first_word_index]["start"]
            )
            timeline.append({
                "block_id": source_block["block_id"],
                "beat_id": beat_id,
                "visual_type": beat.get("visual_type"),
                "description": beat.get("description", beat.get("prompt")),
                **({"prompt": beat["prompt"], "audio": beat["audio"]}
                   if "prompt" in beat else {}),
                **({"video_prompt": beat["video_prompt"]}
                   if "video_prompt" in beat else {}),
                "start_seconds": float(start),
                "first_word_seconds": float(
                    starts[index] + aligned_words[first_word_index]["start"]
                ),
                "last_word_end_seconds": float(
                    starts[index] + aligned_words[last_word_index]["end"]
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



def _media_image_motion_filter(timeline: dict[str, Any]) -> str:
    """Animate media_image beats with deterministic bidirectional Ken Burns."""

    intervals: list[tuple[int, int, str]] = []
    image_index = 0
    for beat in timeline["beats"]:
        if beat["visual_type"] != "media_image":
            continue

        start_frame = beat["start_frame"]
        end_frame = beat["end_frame"]
        if end_frame - start_frame < 2:
            raise StaticPreviewError(
                f"media_image {beat['beat_id']} needs at least two preview "
                "frames for visible motion"
            )

        direction = motion_direction_for_index(image_index)
        image_index += 1
        beat["motion"] = motion_metadata(
            (end_frame - start_frame) / _FPS,
            direction=direction,
        )
        intervals.append((start_frame, end_frame, direction))

    return build_timeline_ken_burns_filter(
        intervals,
        width=_WIDTH,
        height=_HEIGHT,
        fps=_FPS,
    )


def generate_static_preview(
    output: Path, plan: dict[str, Any], audio: dict[str, Any],
    image_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Publish an MP4 only after all STT cuts, PNGs and the audio validate."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise StaticPreviewError("ffmpeg and ffprobe are required for the static preview")
    if image_manifest.get("status") != "completed":
        raise StaticPreviewError("Static preview requires the completed B2 image manifest")
    if plan.get("visual_style") == "profesional" and (
        image_manifest.get("finishing_status") != "completed"
    ):
        raise StaticPreviewError("Professional images must finish before preview")
    grids = [
        item for item in image_manifest.get("items", [])
        if item.get("grid") is not None
    ]
    expected_panels = [
        (panel["block_id"], panel["beat_id"])
        for grid in grids for panel in grid["panels"]
        if panel.get("source") != BLACK_PANEL_SOURCE
    ]
    cropped = image_manifest.get("panel_assets", [])
    if expected_panels and (
        len(cropped) != len(expected_panels)
        or len(set(expected_panels)) != len(expected_panels)
        or {(item["block_id"], item["beat_id"]) for item in cropped}
        != set(expected_panels)
    ):
        raise StaticPreviewError(
            "Grid panel crops are incomplete or do not match the saved B2 panel map"
        )
    timeline = build_stt_beat_timeline(output, plan, audio)
    avatar_meta = plan.get("avatar")
    if not isinstance(avatar_meta, dict):
        raise StaticPreviewError("Static preview requires the saved avatar")
    avatar = _verified_asset(output, avatar_meta.get("file"), avatar_meta.get("sha256"))
    assets: dict[tuple[int, str], Path] = {}
    for item in (
        image_manifest["final_assets"]
        if plan.get("visual_style") == "profesional"
        else [
            *(item for item in image_manifest.get("items", []) if item.get("grid") is None),
            *cropped,
        ]
    ):
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
        presenter_image = avatar
        if beat["visual_type"] == "avatar" and beat.get("prompt") is not None:
            presenter_image = assets.get(key)
            if presenter_image is None:
                raise StaticPreviewError(f"Regenerated avatar {beat['beat_id']} has no PNG")
        _beat_frame(presenter_image, assets.get(key), beat["visual_type"], frame)
        entries.append(f"file 'frames/{frame_name}'")
        entries.append(f"duration {beat['duration_seconds']:.9f}")
    entries.append(entries[-2])  # Repeated last frame preserves its last duration.
    concat.write_text("\n".join(entries) + "\n", encoding="utf-8")
    video_filter = _media_image_motion_filter(timeline)
    # On Windows an inline FFmpeg graph has a command-line size limit.
    # A filter script supports long B2 timelines without changing the graph.
    filter_args = ["-vf", video_filter]
    if len(video_filter) > 16_000:
        filter_script = preview / "ken_burns.fffilter"
        if filter_script.is_symlink():
            raise StaticPreviewError("Refusing linked Ken Burns filter script")
        filter_script.write_text(video_filter + "\n", encoding="utf-8")
        filter_args = ["-filter_script:v", str(filter_script)]
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
            *filter_args,
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
        "media_image_motion": {
            "version": _MOTION_VERSION,
            "animated_beats": sum(
                beat["visual_type"] == "media_image" for beat in timeline["beats"]
            ),
        },
    }
    _save_json(preview / "manifest.json", manifest)
    return manifest
