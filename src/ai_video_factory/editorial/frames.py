"""Read-only sampling of the actual assembled base video for E2."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ai_video_factory.editorial.compiler import resolve_anchor
from ai_video_factory.editorial.contracts import EditorialIntervention
from ai_video_factory.editorial.timeline import RENDER_FPS, rendered_windows


def extract_frames(
    video: Path, output: Path, intervention: EditorialIntervention,
    index: dict, timeline: dict,
) -> list[tuple[str, Path]]:
    windows = rendered_windows(timeline)
    first = round(resolve_anchor(intervention.elements[0].anchor, index) * RENDER_FPS)
    end = windows[intervention.end_beat_id][1]
    if end <= first:
        raise ValueError("Invalid editorial frame sample interval")
    # Sample beginning, middle and end, including each intervening beat midpoint.
    selected = {first, first + (end-first)//2, end - 1}
    ordered = timeline["beats"]
    first_id = intervention.elements[0].anchor.beat_id
    low = next(i for i, beat in enumerate(ordered) if beat["beat_id"] == first_id)
    high = next(i for i, beat in enumerate(ordered) if beat["beat_id"] == intervention.end_beat_id)
    for beat in ordered[low:high + 1]:
        lo = max(first, windows[beat["beat_id"]][0])
        hi = min(end, windows[beat["beat_id"]][1])
        if hi > lo:
            selected.add(lo + (hi - lo)//2)
    selected = sorted(selected)
    if len(selected) > 8:
        selected = [selected[round(i * (len(selected)-1) / 7)] for i in range(8)]
    # Protect against references to different video contents with identical time anchors.
    key = hashlib.sha256(
        (str(video.resolve()) + ":" + str(video.stat().st_size)
         + ":" + str(video.stat().st_mtime_ns)).encode()
    ).hexdigest()[:16]
    folder = output / "editorial" / "frames" / key
    if folder.is_symlink():
        raise ValueError("Unsafe editorial frame cache")
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    for n in sorted(set(selected)):
        image = folder / f"{n:09d}.jpg"
        if image.is_symlink():
            raise ValueError("Unsafe editorial frame")
        if not image.is_file():
            proc = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{n / RENDER_FPS:.9f}", "-i", str(video), "-frames:v", "1",
                "-vf", "scale=960:540", "-q:v", "3", "-y", str(image),
            ], capture_output=True, text=True, check=False)
            if proc.returncode or not image.is_file():
                raise RuntimeError("Cannot extract editorial frame: " + proc.stderr[-400:])
        result.append((f"frame_{n}", image))
    return result
