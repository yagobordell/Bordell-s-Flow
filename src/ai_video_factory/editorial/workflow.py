"""End-to-end professional E1/E2 postproduction without touching paid LTX jobs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import ImageFont

from ai_video_factory.compositor.b_video import _probe_final
from ai_video_factory.compositor.professional_cta_qr import (
    apply_professional_ebook_qr,
    professional_cta_website_windows,
    professional_cta_windows,
)
from ai_video_factory.editorial.compiler import (
    EditorialValidationError,
    compile_plan,
    resolve_anchor,
)
from ai_video_factory.editorial.contracts import (
    E2Output,
    EditorialIntervention,
    LayoutDecision,
)
from ai_video_factory.editorial.directors import _write, run_e1, run_e2
from ai_video_factory.editorial.frames import extract_frames
from ai_video_factory.editorial.word_index import build_word_index
from ai_video_factory.inference.storage import sha256_file


class EditorialError(RuntimeError):
    pass


def preflight_editorial_renderer() -> None:
    package = Path(__file__).resolve().parents[3] / "remotion" / "editorial"
    if shutil.which("node") is None:
        raise EditorialError("Node.js 20+ is required for professional editorial renders")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise EditorialError("FFmpeg and FFprobe are required for editorial postproduction")
    if not (package / "node_modules" / "remotion").is_dir():
        raise EditorialError(
            "Remotion dependencies are missing. Run npm install in remotion/editorial"
        )


def _verified(output: Path, relative: Any, digest: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise EditorialError("Missing editorial source path")
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise EditorialError("Unsafe editorial source path")
    path = output / rel
    if (path.is_symlink() or not path.is_file()
        or not path.resolve().is_relative_to(output.resolve())
        or not isinstance(digest, str) or sha256_file(path) != digest):
        raise EditorialError("Invalid verified editorial source")
    return path


def _relative(output: Path, path: Path) -> str:
    return path.relative_to(output).as_posix()


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _protected(
    start: int, end: int, timeline: dict[str, Any], plan: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for a, b in professional_cta_windows(timeline, plan):
        if start < round(b * 24) and round(a * 24) < end:
            result.append({"kind": "qr", "bounds": [0.83, 0.015, 0.99, 0.31]})
    for a, b in professional_cta_website_windows(timeline, plan):
        if start < round(b * 24) and round(a * 24) < end:
            result.append({"kind": "website", "bounds": [0.30, 0.78, 0.70, 0.90]})
    return result


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return None


def _fits(effect: EditorialIntervention, decision: LayoutDecision) -> bool:
    # The browser uses Arial Bold; compare with a conservative 10% safety pad.
    texts = [item.text for item in effect.elements]
    if effect.kind == "progressive_text":
        texts = [" ".join(texts)]
    elif effect.kind == "accumulating_list":
        texts = [f"{i}. {text}" for i, text in enumerate(texts, 1)]
    font = _font(decision.font_size_px)
    if font is None:
        return False
    widths = [font.getlength(text) for text in texts]
    if effect.kind in {"comparison_cards", "process_flow"}:
        max_width = sum(widths) * 1.14 + (len(widths) - 1) * 90 + 60
    else:
        max_width = max(widths) * 1.12 + 16
    row_count = len(texts) if effect.kind == "accumulating_list" else 1
    height = row_count * decision.font_size_px * 1.45 + 16
    return max_width <= decision.width * 1920 and height <= decision.height * 1080


def _safe_decision(
    effect: EditorialIntervention, decision: LayoutDecision,
    protected: list[dict[str, Any]],
) -> LayoutDecision:
    if decision.status == "suppressed":
        return decision
    region = (decision.x, decision.y, decision.x + decision.width,
              decision.y + decision.height)
    if any(_overlaps(region, tuple(item["bounds"])) for item in protected):
        return decision.model_copy(update={
            "status": "suppressed", "reason": "Protected CTA overlay region intersects text",
        })
    if not _fits(effect, decision):
        return decision.model_copy(update={
            "status": "suppressed", "reason": "Text cannot fit in the approved rectangle",
        })
    return decision


def _verify_e1(
    interventions: list[EditorialIntervention], index: dict[str, Any],
    timeline: dict[str, Any], plan: dict[str, Any],
) -> None:
    beats = {beat["beat_id"]: i for i, beat in enumerate(timeline["beats"])}
    groups = {identifier for group in plan.get("ebook_cta_groups", []) for identifier in group}
    for effect in interventions:
        if effect.end_beat_id not in beats:
            raise EditorialValidationError("Unknown end beat")
        previous = -1.0
        first = None
        for item in effect.elements:
            if item.anchor.beat_id in groups:
                raise EditorialValidationError("CTA-owned beat is protected")
            moment = resolve_anchor(item.anchor, index)
            if moment < previous:
                raise EditorialValidationError("Progressive element order differs from narration")
            previous = moment
            if first is None:
                first = item.anchor.beat_id
        if first is None or beats[effect.end_beat_id] < beats[first]:
            raise EditorialValidationError("Invalid end beat order")


def _render_overlay(plan_file: Path, layers: Path) -> None:
    package = Path(__file__).resolve().parents[3] / "remotion" / "editorial"
    if not (package / "node_modules" / "remotion").is_dir():
        raise EditorialError(
            "Remotion dependencies are missing. Run npm install in remotion/editorial"
        )
    if layers.is_symlink():
        raise EditorialError("Unsafe editorial layers directory")
    process = subprocess.run([
        "node", str(package / "render.mjs"), str(plan_file), str(layers),
    ], cwd=package, capture_output=True, text=True, check=False)
    if process.returncode != 0 or not (layers / "layers.json").is_file():
        raise EditorialError("Remotion layer rendering failed: " + process.stderr[-800:])


def _composite(
    base: Path, layers: Path, target: Path, events: list[dict[str, Any]],
    frames: int, seconds: float,
) -> None:
    manifest_file = layers / "layers.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise EditorialError("Missing verified Remotion layer metadata")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    clips = manifest.get("clips")
    if (
        manifest.get("schema_version") != "editorial-layers-v1"
        or not isinstance(clips, list) or len(clips) != len(events)
    ):
        raise EditorialError("Remotion layer manifest differs from render plan")
    arguments = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y", "-i", str(base),
    ]
    filters = []
    previous = "[0:v]"
    for index, (clip, event) in enumerate(zip(clips, events, strict=True), start=1):
        name = f"layer-{index-1:03d}.mov"
        if (
            clip.get("file") != name or clip.get("id") != event["id"]
            or clip.get("start_frame") != event["start_frame"]
            or clip.get("end_frame") != event["end_frame"]
        ):
            raise EditorialError("Remotion layer is not the requested editorial interval")
        path = layers / name
        if (path.is_symlink() or not path.is_file()
            or sha256_file(path) != clip.get("sha256")):
            raise EditorialError("Missing, linked or modified Remotion overlay")
        arguments.extend(["-i", str(path)])
        label = f"e{index}"
        filters.append(
            f"[{index}:v]setpts=PTS-STARTPTS+"
            f"{event['start_frame']}/(24*TB)[{label}]"
        )
        out = f"[v{index}]"
        filters.append(
            f"{previous}[{label}]overlay=0:0:format=auto:"
            f"repeatlast=0:eof_action=pass{out}"
        )
        previous = out
    filters.append(f"{previous}format=yuv420p[v]")
    temporary = target.with_name(target.stem + "." + uuid4().hex + ".part.mp4")
    try:
        process = subprocess.run([
            *arguments,
            "-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "0:a:0", "-frames:v", str(frames),
            "-r", "24", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(temporary),
        ], capture_output=True, text=True, check=False)
        if process.returncode:
            raise EditorialError(
                "Editorial composition failed: " + process.stderr[-900:]
            )
        _probe_final(temporary, seconds, frames)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def generate_editorial_video(
    output: Path, report: dict[str, Any], *,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Only reads completed LTX media and verified STT; no B, TTS, I2V or A2V calls."""
    plan_path = output / "visual_plan.json"
    if plan_path.is_symlink() or not plan_path.is_file():
        raise EditorialError("Missing immutable visual plan")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("visual_style") != "profesional":
        raise EditorialError("Editorial mode is supported for professional video only")
    run = report["run"]
    if run.get("script_sha256") is None:
        raise EditorialError("Missing source script identity")
    video = run.get("video_generation")
    audio = run.get("audio_generation")
    if not isinstance(video, dict) or video.get("status") != "completed":
        raise EditorialError("Editorial mode requires verified completed LTX video")
    if not isinstance(audio, dict) or audio.get("status") != "completed":
        raise EditorialError("Editorial mode requires completed narration")
    _verified(output, video["file"], video["sha256"])
    timeline_file = video["timeline_file"]
    timeline_path = _verified(output, timeline_file, sha256_file(output / timeline_file))
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    base = timeline_path.parent / "final.mp4"
    if (base.is_symlink() or not base.is_file()
        or not base.resolve().is_relative_to(output.resolve())):
        raise EditorialError("Original pre-CTA LTX video missing")
    _probe_final(
        base, float(timeline["duration_seconds"]), round(timeline["duration_seconds"] * 24),
    )
    index = build_word_index(output, plan, audio)

    async def decide() -> tuple[Any, E2Output, list[str], str]:
        e1, key = await run_e1(output, plan, timeline, provider=provider)
        _verify_e1(e1.interventions, index, timeline, plan)
        decisions = []
        keys = []
        for effect in e1.interventions:
            frames = extract_frames(base, output, effect, index, timeline)
            start = round(resolve_anchor(effect.elements[0].anchor, index) * 24)
            ends = {beat["beat_id"]: beat for beat in timeline["beats"]}
            last = ends[effect.end_beat_id]["end_frame"]
            protected = _protected(start, last, timeline, plan)
            response, e2key = await run_e2(
                output, effect.model_dump(mode="json"), frames, protected,
                provider=provider,
            )
            keys.append(e2key)
            decisions.append(_safe_decision(effect, response.decisions[0], protected))
        return e1, E2Output(schema_version="e2-v1", decisions=decisions), keys, key

    e1, e2, e2keys, e1key = asyncio.run(decide())
    render_plan = compile_plan(e1, e2, index, timeline)
    identity = hashlib.sha256(json.dumps({
        "base_sha": sha256_file(base), "e1": e1key, "e2": e2keys,
        "plan": render_plan, "renderer_version": "remotion-prores-alpha-v1",
    }, sort_keys=True).encode()).hexdigest()
    folder = output / "editorial" / "runs" / identity[:20]
    if folder.is_symlink():
        raise EditorialError("Unsafe editorial run directory")
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    if manifest_path.is_symlink():
        raise EditorialError("Unsafe editorial manifest")
    if manifest_path.is_file():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("fingerprint") != identity:
            raise EditorialError("Editorial manifest identity mismatch")
        _verified(output, old["file"], old["sha256"])
        return old
    plan_file = folder / "render_plan.json"
    _write(plan_file, render_plan)
    result_file = folder / "edited.mp4"
    if result_file.is_symlink():
        raise EditorialError("Unsafe edited video output")
    if not result_file.is_file():
        if render_plan["events"]:
            layers = folder / "layers"
            if not (layers / "layers.json").is_file():
                _render_overlay(plan_file, layers)
            _composite(
                base, layers, result_file, render_plan["events"],
                render_plan["duration_frames"], timeline["duration_seconds"],
            )
        else:
            shutil.copyfile(base, result_file)
    _probe_final(result_file, timeline["duration_seconds"], render_plan["duration_frames"])
    rendered = {
        "file": _relative(output, result_file), "sha256": sha256_file(result_file),
        "duration_seconds": timeline["duration_seconds"],
    }
    if plan.get("ebook_cta_groups"):
        rendered = apply_professional_ebook_qr(output, folder, rendered, timeline, plan)
    result = {
        "schema_version": "editorial-video-v1",
        "status": "completed", "fingerprint": identity,
        "source_video_sha256": video["sha256"],
        "source_audio_sha256": audio["sha256"],
        "render_plan_file": _relative(output, plan_file),
        "interventions": len(e1.interventions),
        "approved_effects": len(render_plan["events"]),
        **rendered,
    }
    _write(manifest_path, result)
    _write(output / "editorial" / "latest.json", {
        "manifest_file": _relative(output, manifest_path),
        "fingerprint": identity, "file": result["file"], "sha256": result["sha256"],
    })
    return result
