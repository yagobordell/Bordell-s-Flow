"""Frame-accurate, scan-safe QR overlay for professional ebook CTA beats."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.inference.storage import sha256_file


class ProfessionalCTAOverlayError(RuntimeError):
    pass


def professional_cta_windows(
    timeline: dict[str, Any], plan: dict[str, Any],
) -> list[tuple[float, float]]:
    mapping = {
        beat["beat_id"]: beat for beat in timeline["beats"]
    }
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


def apply_professional_ebook_qr(
    output: Path, run_dir: Path,
    rendered: dict[str, Any], timeline: dict[str, Any], plan: dict[str, Any],
) -> dict[str, Any]:
    windows = professional_cta_windows(timeline, plan)
    if not windows:
        return rendered
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
    # A static QR image is never sent through GPT Images or LTX. Never animate
    # its pixels. These windows use real STT boundaries, not guessed beat times.
    enabled = "+".join(
        f"between(t\\,{start:.6f}\\,{end:.6f})" for start, end in windows
    )
    filter_graph = (
        "[1:v]scale=280:280:flags=neighbor,format=rgba[qr];"
        "[0:v][qr]overlay=x=main_w-overlay_w-34:y=34:"
        f"enable='{enabled}':eval=frame,format=yuv420p[v]"
    )
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(source), "-loop", "1", "-i", str(qr),
                "-filter_complex", filter_graph,
                "-map", "[v]", "-map", "0:a:0", "-frames:v",
                str(round(float(timeline["duration_seconds"]) * 24)),
                "-r", "24", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "copy",
                "-movflags", "+faststart", str(temporary),
            ],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise ProfessionalCTAOverlayError(
                "CTA QR FFmpeg failed: " + proc.stderr[-1000:]
            )
        from ai_video_factory.compositor.b_video import _probe_final

        _probe_final(
            temporary, float(timeline["duration_seconds"]),
            round(float(timeline["duration_seconds"]) * 24),
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
            "windows_seconds": [[start, end] for start, end in windows],
        },
    }
