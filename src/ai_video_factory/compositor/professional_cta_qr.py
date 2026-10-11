"""Frame-accurate QR and spoken-QR-triggered website overlays for ebook CTAs."""

from __future__ import annotations

import io
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ai_video_factory.inference.storage import sha256_file

_WEBSITE = "zellenne.com"
_WEBSITE_FONT_PX = 46
_WEBSITE_STROKE_PX = 2
_WEBSITE_BOTTOM_MARGIN_PX = 145
_WEBSITE_VERSION = 1
_FPS = 24


class ProfessionalCTAOverlayError(RuntimeError):
    pass


def professional_cta_windows(
    timeline: dict[str, Any], plan: dict[str, Any],
) -> list[tuple[float, float]]:
    mapping = {beat["beat_id"]: beat for beat in timeline["beats"]}
    groups = plan.get("ebook_cta_groups") or []
    duration = float(timeline["duration_seconds"])
    windows = []
    for index, ids in enumerate(groups):
        if not ids or any(beat_id not in mapping for beat_id in ids):
            raise ProfessionalCTAOverlayError("Invalid CTA group in saved plan")
        start = float(mapping[ids[0]]["start_seconds"])
        end = float(mapping[ids[-1]]["end_seconds"])
        if start < 0 or end <= start or end > duration + 0.05:
            raise ProfessionalCTAOverlayError("Invalid CTA audio time window")
        if index == 0:
            end = min(duration, end + 90.0)
        windows.append((start, end))
    return windows


def professional_cta_website_windows(
    timeline: dict[str, Any], plan: dict[str, Any],
) -> list[tuple[float, float]]:
    """From the first spoken 'QR' of each CTA until that CTA ends, never +90s.

    Word onsets were already aligned to verified Whisper STT inside the
    canonical beat timeline. Never estimate the start from beat boundaries or
    infer the CTA from a hard-coded phrase.
    """
    beats = {beat["beat_id"]: beat for beat in timeline["beats"]}
    windows: list[tuple[float, float]] = []
    for group in plan.get("ebook_cta_groups") or []:
        if not group or any(beat_id not in beats for beat_id in group):
            raise ProfessionalCTAOverlayError("Invalid website CTA group")
        end = float(beats[group[-1]]["end_seconds"])
        spoken: list[float] = []
        for beat_id in group:
            beat = beats[beat_id]
            for onset in beat.get("qr_spoken_seconds", []):
                if (
                    isinstance(onset, bool)
                    or not isinstance(onset, (int, float))
                    or not math.isfinite(onset)
                    or onset < beat["start_seconds"] - 0.05
                    or onset > beat["end_seconds"] + 0.05
                ):
                    raise ProfessionalCTAOverlayError(
                        f"Invalid STT QR onset within CTA beat {beat_id}"
                    )
                spoken.append(float(onset))
        if not spoken:
            continue  # An ebook CTA with no spoken "QR" gets no website text.
        start = min(spoken)
        if start >= end:
            raise ProfessionalCTAOverlayError("CTA ends before spoken QR")
        windows.append((start, end))
    return windows


def _website_font() -> ImageFont.FreeTypeFont:
    # Match the Windows/Linux system fonts already used by the CTA compositor.
    for name in (
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ):
        if Path(name).is_file():
            return ImageFont.truetype(name, _WEBSITE_FONT_PX)
    raise ProfessionalCTAOverlayError("No suitable font for professional CTA website")


def _website_card(path: Path) -> None:
    """Rasterize exact copy once; white text, fine black outline, no AI text."""
    font = _website_font()
    measure = Image.new("RGBA", (1, 1))
    bounds = ImageDraw.Draw(measure).textbbox(
        (0, 0), _WEBSITE, font=font, stroke_width=_WEBSITE_STROKE_PX,
    )
    pad = 8
    card = Image.new(
        "RGBA",
        (bounds[2] - bounds[0] + 2 * pad, bounds[3] - bounds[1] + 2 * pad),
    )
    ImageDraw.Draw(card).text(
        (pad - bounds[0], pad - bounds[1]), _WEBSITE, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=_WEBSITE_STROKE_PX,
        stroke_fill=(0, 0, 0, 255),
    )
    stream = io.BytesIO()
    card.save(stream, format="PNG")
    payload = stream.getvalue()
    if path.is_symlink():
        raise ProfessionalCTAOverlayError("Refusing symlinked CTA website image")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ProfessionalCTAOverlayError("Existing CTA website image differs")
        return
    temporary = path.with_suffix(".part.png")
    if temporary.exists() or temporary.is_symlink():
        raise ProfessionalCTAOverlayError("Unreconciled CTA website image")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _enable_windows(
    windows: list[tuple[float, float]], *, inclusive_end: bool,
) -> str:
    if inclusive_end:
        return "+".join(
            f"between(t\\,{start:.6f}\\,{end:.6f})"
            for start, end in windows
        )
    return "+".join(
        f"gte(t\\,{start:.6f})*lt(t\\,{end:.6f})"
        for start, end in windows
    )


def build_professional_cta_mux_overlay(
    output: Path,
    run_dir: Path,
    timeline: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any]] | None:
    """Prepare QR/website inputs for the EXISTING final x264 encode.

    Inputs 0 and 1 are the concatenated video and original PCM narration.
    Extra overlay inputs start at 2. Do not re-encode the finished MP4.
    The same CTA window calculations and rasterized website as the legacy
    two-pass compositor are used; no paid inference inputs are changed.
    """
    qr_windows = professional_cta_windows(timeline, plan)
    if not qr_windows:
        return None
    website_windows = professional_cta_website_windows(timeline, plan)
    qr = output / "qr.png"
    qr_meta = plan.get("avatar", {}).get("qr", {})
    if qr.is_symlink() or not qr.is_file():
        raise ProfessionalCTAOverlayError("Missing safe persisted QR file")
    if not isinstance(qr_meta, dict) or qr_meta.get("sha256") != sha256_file(qr):
        raise ProfessionalCTAOverlayError("Saved QR does not match avatar identity")
    with Image.open(qr) as image:
        if image.format != "PNG":
            raise ProfessionalCTAOverlayError("QR must be a real PNG")
        image.verify()

    inputs = ["-loop", "1", "-i", str(qr)]
    # Renumber video frames BEFORE applying timestamp-windowed overlays,
    # exactly as the intermediate final.mp4 did in the original two-pass path.
    graph = (
        f"[0:v]setpts=N/({_FPS}*TB),fps={_FPS},format=yuv420p[base];"
        "[2:v]scale=280:280:flags=neighbor,format=rgba[qr];"
        "[base][qr]overlay=x=main_w-overlay_w-34:y=34:"
        f"enable='{_enable_windows(qr_windows, inclusive_end=True)}':"
        f"eval=frame:format={'auto' if website_windows else 'yuv420'}"
        f"[{'qr_base' if website_windows else 'v'}]"
    )
    if website_windows:
        card = run_dir / "ebook_cta_website.png"
        _website_card(card)
        inputs.extend(["-loop", "1", "-i", str(card)])
        graph += (
            ";[3:v]format=rgba[website];"
            "[qr_base][website]overlay="
            "x=(main_w-overlay_w)/2:"
            f"y=main_h-overlay_h-{_WEBSITE_BOTTOM_MARGIN_PX}:"
            f"enable='{_enable_windows(website_windows, inclusive_end=False)}':"
            "eval=frame:format=yuv420[v]"
        )

    metadata = {
        "qr_sha256": qr_meta["sha256"],
        "position": "top_right",
        "size_pixels": 280,
        "first_cta_tail_seconds": 90,
        "windows_seconds": [[start, end] for start, end in qr_windows],
        "website_overlay": {
            "version": _WEBSITE_VERSION,
            "text": _WEBSITE,
            "position": "bottom_center_above_subtitles",
            "font_size_pixels": _WEBSITE_FONT_PX,
            "black_outline_pixels": _WEBSITE_STROKE_PX,
            "bottom_margin_pixels": _WEBSITE_BOTTOM_MARGIN_PX,
            "windows_seconds": [[start, end] for start, end in website_windows],
        },
    }
    return inputs, graph, metadata


def apply_professional_ebook_qr(
    output: Path, run_dir: Path,
    rendered: dict[str, Any], timeline: dict[str, Any], plan: dict[str, Any],
) -> dict[str, Any]:
    qr_windows = professional_cta_windows(timeline, plan)
    if not qr_windows:
        return rendered
    website_windows = professional_cta_website_windows(timeline, plan)
    qr_meta = plan.get("avatar", {}).get("qr", {})
    qr = output / "qr.png"
    if qr.is_symlink() or not qr.is_file():
        raise ProfessionalCTAOverlayError("Missing safe persisted QR file")
    if not isinstance(qr_meta, dict) or qr_meta.get("sha256") != sha256_file(qr):
        raise ProfessionalCTAOverlayError("Saved QR does not match avatar identity")
    with Image.open(qr) as image:
        if image.format != "PNG":
            raise ProfessionalCTAOverlayError("QR must be a real PNG")
        image.verify()
    source = output / rendered["file"]
    if source.is_symlink() or not source.is_file():
        raise ProfessionalCTAOverlayError("Missing verified pre-overlay video")
    if sha256_file(source) != rendered["sha256"]:
        raise ProfessionalCTAOverlayError("Pre-overlay video SHA mismatch")

    destination = run_dir / "with_ebook_qr.mp4"
    temporary = run_dir / "with_ebook_qr.part.mp4"
    if destination.is_symlink() or temporary.is_symlink() or temporary.exists():
        raise ProfessionalCTAOverlayError("Unsafe or unreconciled CTA overlay output")

    # Keep the QR visible through its existing 90-second tail. Draw the
    # website in a second independent overlay only during spoken-QR CTA time.
    filter_graph = (
        "[1:v]scale=280:280:flags=neighbor,format=rgba[qr];"
        "[0:v][qr]overlay=x=main_w-overlay_w-34:y=34:"
        f"enable='{_enable_windows(qr_windows, inclusive_end=True)}':"
        f"eval=frame:format={'auto' if website_windows else 'yuv420'}"
        f"[{'qr_base' if website_windows else 'v'}]"
    )
    arguments = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-loop", "1", "-i", str(qr),
    ]
    if website_windows:
        card = run_dir / "ebook_cta_website.png"
        _website_card(card)
        arguments.extend(["-loop", "1", "-i", str(card)])
        filter_graph += (
            ";[2:v]format=rgba[website];"
            "[qr_base][website]overlay="
            "x=(main_w-overlay_w)/2:"
            f"y=main_h-overlay_h-{_WEBSITE_BOTTOM_MARGIN_PX}:"
            f"enable='{_enable_windows(website_windows, inclusive_end=False)}':"
            "eval=frame:format=yuv420[v]"
        )
    arguments.extend([
        "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "0:a:0", "-frames:v",
        str(round(float(timeline["duration_seconds"]) * _FPS)),
        "-r", str(_FPS), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", str(temporary),
    ])
    try:
        proc = subprocess.run(
            arguments, capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise ProfessionalCTAOverlayError(
                "CTA QR/website FFmpeg failed: " + proc.stderr[-1000:]
            )
        from ai_video_factory.compositor.b_video import _probe_final

        _probe_final(
            temporary, float(timeline["duration_seconds"]),
            round(float(timeline["duration_seconds"]) * _FPS),
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **rendered,
        "file": destination.relative_to(output).as_posix(),
        "sha256": sha256_file(destination),
        "professional_ebook_qr": {
            "qr_sha256": qr_meta["sha256"],
            "position": "top_right",
            "size_pixels": 280,
            "first_cta_tail_seconds": 90,
            "windows_seconds": [[start, end] for start, end in qr_windows],
            "website_overlay": {
                "version": _WEBSITE_VERSION,
                "text": _WEBSITE,
                "position": "bottom_center_above_subtitles",
                "font_size_pixels": _WEBSITE_FONT_PX,
                "black_outline_pixels": _WEBSITE_STROKE_PX,
                "bottom_margin_pixels": _WEBSITE_BOTTOM_MARGIN_PX,
                "windows_seconds": [
                    [start, end] for start, end in website_windows
                ],
            },
        },
    }
