"""Single-line white captions with a black drop shadow for casero videos.

Runs locally *after* CTA insertion; the CTA has no subtitle events.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

from ai_video_factory.compositor.static_preview import _verified_asset
from ai_video_factory.inference.storage import sha256_file


class CaseroSubtitleError(RuntimeError):
    """The saved captions or their authoritative timing cannot be verified."""


def _read_words(output: Path, audio: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = audio.get("blocks")
    offsets = audio.get("block_start_seconds")
    if not isinstance(blocks, list) or not isinstance(offsets, list) or len(blocks) != len(offsets):
        raise CaseroSubtitleError("Missing saved narration STT block offsets")
    words: list[dict[str, Any]] = []
    for block, offset in zip(blocks, offsets, strict=True):
        stt = block.get("stt")
        if not isinstance(stt, dict):
            raise CaseroSubtitleError("Narration block missing STT")
        source = _verified_asset(output, stt.get("file"), stt.get("sha256"))
        transcription = json.loads(source.read_text(encoding="utf-8"))
        if transcription.get("source_audio_sha256") != block.get("sha256"):
            raise CaseroSubtitleError("STT transcript belongs to another audio block")
        for word in transcription.get("words", []):
            try:
                text = word["word"].strip()
                start = float(offset) + float(word["start"])
                end = float(offset) + float(word["end"])
            except (KeyError, ValueError, TypeError) as exc:
                raise CaseroSubtitleError("Invalid caption word timing") from exc
            if (not text or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end < start):
                raise CaseroSubtitleError("Invalid STT word interval")
            words.append({"text": text, "start": start, "end": end})
    return words


def _shift_around_cta(
    words: list[dict[str, Any]], insertion: dict[str, Any] | None,
) -> list[list[dict[str, Any]]]:
    if not insertion:
        return [words]
    cut = float(insertion["video_cut_seconds"])
    boundary = float(insertion.get("source_boundary_seconds", cut))
    duration = float(insertion["cta_duration_seconds"])
    if (not all(math.isfinite(x) for x in (cut, boundary, duration))
            or cut < 0 or boundary < 0 or duration <= 0):
        raise CaseroSubtitleError("Invalid CTA insertion timing")
    before, after = [], []
    for word in words:
        # Source words are partitioned by the actual aligned boundary, not the
        # snapped video frame. These may differ by up to half a frame at 24fps.
        if word["start"] < boundary:
            end = min(word["end"], cut)
            if word["start"] < end:
                before.append({**word, "end": end})
        else:
            start = max(word["start"], cut) + duration
            end = max(word["end"], cut) + duration
            if end > start:
                after.append({**word, "start": start, "end": end})
    # A hard boundary prevents captions overlapping the CTA insert.
    return [before, after]


def _flush_caption(
    pending: list[dict[str, Any]], captions: list[dict[str, Any]],
) -> None:
    if not pending:
        return
    start, end = pending[0]["start"], pending[-1]["end"]
    captions.append({
        "start": start, "end": max(end, start + 0.12),
        "text": " ".join(word["text"] for word in pending),
    })
    pending.clear()


def _captions(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    captions: list[dict[str, Any]] = []
    # 48 characters at 30px fits comfortably in a 1920px frame.
    max_chars, max_words, max_seconds = 48, 9, 3.3
    for words in groups:
        pending: list[dict[str, Any]] = []
        for word in words:
            candidate = " ".join([*(w["text"] for w in pending), word["text"]])
            if pending and (
                len(candidate) > max_chars or len(pending) >= max_words
                or word["end"] - pending[0]["start"] > max_seconds
                or word["start"] - pending[-1]["end"] > 0.48
            ):
                _flush_caption(pending, captions)
            pending.append(word)
            if word["text"].endswith((".", "!", "?", ";", ":")):
                _flush_caption(pending, captions)
        _flush_caption(pending, captions)
    return captions


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _ass_time(seconds: float) -> str:
    centis = round(seconds * 100)
    h, centis = divmod(centis, 360_000)
    m, centis = divmod(centis, 6000)
    s, centis = divmod(centis, 100)
    return f"{h}:{m:02}:{s:02}.{centis:02}"


def _subtitle_files(captions: list[dict[str, Any]]) -> tuple[str, str]:
    srt = "".join(
        f"{n}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n\n"
        for n, c in enumerate(captions, 1)
    )
    # &H00000000 is an opaque black shadow; no opaque box or outline.
    ass = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Arial,30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,2,2,65,65,60,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    for c in captions:
        # Never allow transcription content to inject ASS override tags or line breaks.
        clean = c["text"].replace("\\", "").replace("{", "(").replace("}", ")")
        clean = clean.replace("\r", " ").replace("\n", " ")
        ass += (
            f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},"
            f"Caption,,0,0,0,,{{\\q2}}{clean}\n"
        )
    return srt, ass


def burn_casero_subtitles(
    output: Path, run_dir: Path, video: dict[str, Any],
    audio: dict[str, Any],
) -> dict[str, Any]:
    """Keep the original MP4 and return a verified, independently rendered copy."""
    original = _verified_asset(output, video["file"], video["sha256"])
    source_sha = sha256_file(original)
    target = run_dir / "with_subtitles.mp4"
    srt = run_dir / "subtitles.srt"
    ass = run_dir / "subtitles.ass"
    checkpoint = run_dir / "subtitles_state.json"
    insertion = video.get("cta_insertion")
    if checkpoint.is_file():
        record = json.loads(checkpoint.read_text(encoding="utf-8"))
        if (record.get("source_sha256") != source_sha
                or record.get("audio_sha256") != audio.get("sha256")
                or record.get("cta_insertion") != insertion):
            raise CaseroSubtitleError("Saved subtitles were made from different inputs")
        for path, key in [(target, "video_sha256"), (srt, "srt_sha256"), (ass, "ass_sha256")]:
            if path.is_symlink() or not path.is_file() or sha256_file(path) != record.get(key):
                raise CaseroSubtitleError(f"Saved subtitle artifact changed: {path}")
    else:
        for path in (target, srt, ass, checkpoint, run_dir / "with_subtitles.part.mp4"):
            if path.exists() or path.is_symlink():
                raise CaseroSubtitleError(f"Unreconciled subtitle artifact: {path}")
        words = _read_words(output, audio)
        captions = _captions(_shift_around_cta(words, insertion))
        if not captions:
            raise CaseroSubtitleError("Narration STT has no caption words")
        srt_text, ass_text = _subtitle_files(captions)
        srt.write_text(srt_text, encoding="utf-8")
        ass.write_text(ass_text, encoding="utf-8")
        temp = run_dir / "with_subtitles.part.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(original.resolve()), "-vf", "ass=subtitles.ass",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
            str(temp.resolve()),
        ]
        result = subprocess.run(command, cwd=run_dir, capture_output=True, text=True)
        if result.returncode or not temp.is_file():
            raise CaseroSubtitleError(
                "Cannot burn casero subtitles (FFmpeg with libass required): "
                + result.stderr[-1000:]
            )
        temp.replace(target)
        record = {
            "schema_version": 1, "source_sha256": source_sha,
            "audio_sha256": audio["sha256"], "cta_insertion": insertion,
            "video_sha256": sha256_file(target),
            "srt_sha256": sha256_file(srt), "ass_sha256": sha256_file(ass),
            "caption_count": len(captions),
        }
        tmp = checkpoint.with_name("subtitles_state.json.part")
        if tmp.exists() or tmp.is_symlink():
            raise CaseroSubtitleError("Unreconciled subtitle checkpoint")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(checkpoint)
    return {
        "file": target.relative_to(output).as_posix(),
        "sha256": record["video_sha256"],
        "subtitles": {
            "enabled": True, "style": "single-line-white-black-shadow-v1",
            "srt_file": srt.relative_to(output).as_posix(),
            "srt_sha256": record["srt_sha256"],
            "ass_file": ass.relative_to(output).as_posix(),
            "ass_sha256": record["ass_sha256"],
            "caption_count": record["caption_count"],
        },
    }
