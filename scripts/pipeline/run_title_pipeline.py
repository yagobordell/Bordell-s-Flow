"""Managed Titulo -> T1/T2 -> existing Professional V/audio/image/LTX runner.

The input TXT is a video TITLE, never prewritten narration. Source snapshots,
Claude raw responses and generated B2 material are deterministic checkpoints.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter

from PIL import Image

from ai_video_factory.bots import title_flow
from ai_video_factory.bots.professional_voice import generate_professional_audio
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.providers.avatar_qr import ensure_avatar_qr
from ai_video_factory.providers.professional_avatar_voice import ensure_professional_avatar_voice

try:
    from scripts.pipeline import run_b_pipeline as professional
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.pipeline"}:
        raise
    import run_b_pipeline as professional


def _regular_png(path: Path) -> bytes:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise SystemExit(f"Missing or linked PNG: {path}")
    data = path.read_bytes()
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as img:
            if img.format != "PNG":
                raise ValueError("Not PNG")
            img.verify()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid PNG: {path}") from exc
    return data


def _snapshot(output: Path, relative: str, data: bytes) -> None:
    dest = output / relative
    if dest.is_symlink() or dest.parent.is_symlink():
        raise SystemExit(f"Linked snapshot: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if not dest.is_file() or dest.read_bytes() != data:
            raise SystemExit(f"Title source snapshot changed: {dest}")
    else:
        dest.write_bytes(data)


def _avatar_meta(
    avatar: Path, original: bytes, voice: dict[str, str], qr: object,
) -> dict[str, object]:
    with Image.open(avatar) as image:
        width, height = image.size
    return {
        "filename": avatar.name, "source": avatar.as_posix(), "file": "avatar.png",
        "sha256": title_flow.sha(original), "width": width, "height": height,
        "voice": voice,
        "qr": {
            "file": "qr.png", "url": qr.url, "sha256": qr.sha256,
            "generator": qr.generator, "verification": "deterministic_qr_encoding",
        },
    }


def _cta_groups(blocks: list[dict[str, object]]) -> list[list[str]]:
    groups: dict[int, list[str]] = {}
    for block in blocks:
        for beat in block["beats"]:
            number = beat.get("cta_group_id")
            if number is not None:
                groups.setdefault(number, []).append(beat["beat_id"])
    if sorted(groups) != list(range(1, len(groups) + 1)):
        raise title_flow.TitleContractError("Noncontiguous CTA group ids")
    return [groups[i] for i in sorted(groups)]


def _image_plan_annotations(manifest: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    """Recreate ONLY the image annotations written by _generate_images.

    They are produced after expensive image generation and are not part of
    Claude's immutable T2 plan. Check their provenance against the saved
    completed image manifest before accepting a resumed visual plan.
    """
    if manifest.get("status") != "completed" or manifest.get("finishing_status") != "completed":
        raise title_flow.TitleContractError(
            "Saved visual plan contains image assets without a completed image manifest"
        )
    final_assets = manifest.get("final_assets")
    items = manifest.get("items")
    if not isinstance(final_assets, list) or not isinstance(items, list):
        raise title_flow.TitleContractError("Malformed completed image manifest")
    try:
        image_assets = [
            {
                "block_id": item["block_id"],
                "beat_id": item["beat_id"],
                "asset_id": item.get("asset_id", item["beat_id"]),
                **(
                    {
                        "subshot_index": item["subshot_index"],
                        "subshot_count": item["subshot_count"],
                    }
                    if isinstance(item.get("subshot_index"), int)
                    and isinstance(item.get("subshot_count"), int)
                    else {}
                ),
                "file": item["file"],
                "sha256": item["sha256"],
                "provider": item.get("provider", "ai33"),
            }
            for item in final_assets
        ]
        image_grids = [
            {
                "file": item["file"],
                "sha256": item["sha256"],
                "provider": item["provider"],
                "size": item["size"],
                "grid": item["grid"],
                "panels": item["panels"],
            }
            for item in items if item.get("grid") is not None
        ]
    except (KeyError, TypeError, AttributeError) as exc:
        raise title_flow.TitleContractError("Malformed saved image annotations") from exc
    return {"image_assets": image_assets, "image_grids": image_grids}


def _reconcile_visual_plan(
    output: Path, expected: dict[str, object],
) -> dict[str, object]:
    """Preserve completed image annotations on Title resume, without changing T2.

    T2 --stop saves the bare plan. First full production adds avatar/QR
    metadata, and _generate_images saves image_assets/image_grids. Older
    Title resumes mistakenly compared this enriched plan with the bare plan
    and rejected every otherwise valid post-image LTX restart.
    """
    path = output / "visual_plan.json"
    if path.is_symlink():
        raise title_flow.TitleContractError("Linked visual plan")
    if not path.is_file():
        title_flow.write(path, expected)
        return expected
    saved = title_flow.read(path)
    if saved == expected:
        return saved
    bare = {key: value for key, value in expected.items() if key != "avatar"}
    if saved == bare:
        title_flow.write(path, expected)
        return expected

    # Post-image plan must contain *exactly* the immutable planning fields
    # plus the two documented annotations. Changes in avatar, CTA, beats,
    # or unknown fields remain a hard error rather than being overwritten.
    annotation_names = {"image_assets", "image_grids"}
    if (
        set(saved) != set(expected) | annotation_names
        or {key: value for key, value in saved.items()
            if key not in annotation_names} != expected
    ):
        raise title_flow.TitleContractError(
            "Saved visual plan differs from immutable T2/voice/avatar/QR"
        )
    manifest_path = output / "images" / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise title_flow.TitleContractError(
            "Image-enriched visual plan has no saved image manifest"
        )
    manifest = title_flow.read(manifest_path)
    if {key: saved[key] for key in annotation_names} != _image_plan_annotations(manifest):
        raise title_flow.TitleContractError(
            "Saved image annotations differ from completed image manifest"
        )
    # Do not rewrite or strip annotations: video job IDs and cached assets
    # are derived from the completed, checkpointed image plan.
    return saved


def _compat(output: Path, merged: dict[str, object]) -> None:
    """Deterministic compatibility artifacts; NO B bots are called."""
    blocks = merged["blocks"]
    core = merged["narrative_core"]
    b11 = {
        "pipeline_stage": "B1.1", "narrative_core": core,
        "blocks": [{
            key: block[key] for key in
            ("block_id", "type", "emotional_entry", "emotional_exit", "span")
        } for block in blocks],
    }
    b12 = {
        "pipeline_stage": "B1.2", "narrative_core": core,
        "blocks": [
            {"block_id": block["block_id"], "beats": [{
                key: beat[key] for key in ("beat_id", "text", "beat_type")
            } for beat in block["beats"]]}
            for block in blocks
        ],
    }
    for stage, value in (("B1.1", b11), ("B1.2", b12), ("B2", merged)):
        filename = output / stage / "merged_output.json"
        if filename.is_file() and title_flow.read(filename) != value:
            raise title_flow.TitleContractError(f"Existing {stage} derived artifact changed")
        if not filename.is_file():
            title_flow.write(filename, value)
    transcript = "".join(beat["text"] for block in blocks for beat in block["beats"])
    _snapshot(output, "narracion.txt", transcript.encode("utf-8"))


def _report(
    output: Path, run: dict[str, object], status: str,
    started: float,
) -> None:
    costs: dict[str, object] = {
        "provider": "anthropic", "model": title_flow.MODEL,
        "estimated_total_usd": None, "stages": {},
    }
    stage_usage: dict[str, dict[str, int]] = {}
    for stage in ("T1", "T2"):
        folder = output / stage
        if folder.is_dir():
            responses = sorted(folder.rglob("response_attempt_*.json"))
            for response in responses:
                payload = title_flow.read(response)
                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    metric = stage_usage.setdefault(stage, {})
                    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens"):
                        metric[key] = metric.get(key, 0) + int(usage.get(key) or 0)
    costs["anthropic_usage"] = stage_usage
    report = {
        "run": {**run, "status": status},
        "api_costs": costs,
        "timings": {"status": status, "run_elapsed_seconds": perf_counter() - started},
    }
    title_flow.write(output / "run_report.json", report)


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title_file", type=Path)
    parser.add_argument("--avatar", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--thumbnail")
    parser.add_argument("--output", type=Path, default=Path(".video_factory_work"))
    parser.add_argument("--managed-target", type=Path)
    parser.add_argument("--results-root", type=Path, default=Path("data/results"))
    parser.add_argument("--stop", choices=("T1", "T2"))
    parser.add_argument("--from", dest="from_stage", choices=("T2",))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--no-image", action="store_true")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--qr-url")
    parser.add_argument("--voice-id")
    parser.add_argument("--max-parallel-calls", type=int, default=8)
    args = parser.parse_args(argv)
    started = perf_counter()
    script = args.title_file
    if script.is_symlink() or not script.is_file():
        raise SystemExit("Title TXT is missing or linked")
    raw = script.read_bytes()
    try:
        title = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit("Title TXT must be UTF-8") from exc
    if not title or len(title.splitlines()) != 1:
        raise SystemExit("Title TXT must contain one nonempty title line")
    if args.max_parallel_calls < 1:
        raise SystemExit("--max-parallel-calls must be positive")
    avatar = args.data_root / args.avatar / (args.avatar + ".png")
    avatar_bytes = _regular_png(avatar)
    ebook_record = avatar.parent / "meta" / "ebook_covers.json"
    if ebook_record.is_symlink() or not ebook_record.is_file():
        raise SystemExit("Titulo requires an ebook already associated with the avatar")
    if ebook_record.is_symlink():
        raise SystemExit("Linked ebook cover registry")
    association = json.loads(ebook_record.read_text(encoding="utf-8"))
    stored = association.get("covers", [])
    if (
        association.get("schema_version") != "ebook-covers-v1"
        or association.get("avatar") != avatar.name
        or not isinstance(stored, list) or len(stored) not in (1, 2)
        or not isinstance(stored[0].get("sha256"), str)
    ):
        raise SystemExit("Avatar ebook association is malformed")
    # Only the primary cover matters to Titulo. A damaged SECOND cover
    # cannot block a run that never reads or sends it.
    ebook = _regular_png(avatar.parent / "meta" / "ebook" / "cover_1.png")
    if title_flow.sha(ebook) != stored[0]["sha256"]:
        raise SystemExit("Primary ebook cover differs from registered SHA-256")
    output = args.output / script.stem
    if output.is_symlink() or output.is_junction():
        raise SystemExit("Linked title work directory")
    if output.exists() and not (
        (output / "T1" / "input.json").is_file()
        or (output / "T1" / "inputs" / "title.txt").is_file()
    ):
        raise SystemExit("Existing work folder is not a verified Title run")
    if args.from_stage == "T2" and not (output / "T1" / "output.json").is_file():
        raise SystemExit("--from T2 requires a completed, validated T1 checkpoint")
    output.mkdir(parents=True, exist_ok=True)
    if args.thumbnail is not None:
        name = args.thumbnail
        if name.endswith(".png"):
            name = name[:-4]
        if not name or Path(name).name != name or name.startswith("."):
            raise SystemExit("Thumbnail must be a simple name without .png")
        thumbnail = _regular_png(args.data_root / (name + ".png"))
    else:
        thumb_path = output / "T1" / "inputs" / "thumbnail.png"
        thumbnail = _regular_png(thumb_path)
    _snapshot(output, "T1/inputs/thumbnail.png", thumbnail)
    _snapshot(output, "T1/inputs/cover_1.png", ebook)
    _snapshot(output, "T1/inputs/avatar.png", avatar_bytes)
    _snapshot(output, "T1/inputs/title.txt", raw)
    _snapshot(output, "ebook/cover_1.png", ebook)
    _snapshot(output, "avatar.png", avatar_bytes)
    _snapshot(output, "thumbnail.png", thumbnail)
    metadata: dict[str, object] = {
        "flow": "titulo", "script_file": script.name,
        "script_sha256": title_flow.sha(raw), "visual_style": "profesional",
        "model": title_flow.MODEL, "reasoning_effort": "high",
        "thumbnail_sha256": title_flow.sha(thumbnail),
    }
    if args.stop is None:
        if (
            not settings.openai_api_key and (not args.no_audio or not args.no_image)
        ) or (not settings.gemini_api_key and not args.no_audio):
            raise SystemExit(
                "OPENAI_API_KEY and GEMINI_API_KEY are required for enabled "
                "Professional Title production stages"
            )
        voice_file = avatar.parent / "meta" / "gemini_voice.json"
        if (not args.no_audio and not voice_file.is_file()
                and not sys.stdin.isatty() and args.voice_id is None):
            raise SystemExit(
                "Titulo requires --voice-id on the first noninteractive "
                "Gemini audio run, before any paid T1/T2 request"
            )
    api_key = os.getenv("ANTHROPIC_API_KEY") or professional._runtime_env_value("ANTHROPIC_API_KEY")
    checkpoint_available = (
        (args.stop == "T1" and (output / "T1" / "output.json").is_file())
        or (output / "T2" / "merged_output.json").is_file()
    )
    if not api_key and not checkpoint_available:
        raise SystemExit("ANTHROPIC_API_KEY is required for uncached T1/T2")
    try:
        if (output / "run_report.json").is_file():
            previous = title_flow.read(output / "run_report.json")
            if previous.get("run", {}).get("flow") != "titulo":
                raise SystemExit("Work checkpoint belongs to another flow")
            old_run = previous["run"]
            if old_run.get("script_sha256") != metadata["script_sha256"]:
                raise SystemExit("Saved Title source SHA differs")
            if old_run.get("video_generation", {}).get("status") == "completed":
                print("Verified completed Title production retained; not regenerating.")
                return
        plan, result = title_flow.run(
            output=output, avatar=args.avatar, title=title,
            ebook=ebook, thumbnail=thumbnail, api_key=api_key or "",
            stop=args.stop,
        )
        metadata["planner_stages"] = len(plan["stages"])
        if result is None:
            _report(output, metadata, "stopped_T1", started)
            print("T1 completed. Use --stop T2 or resume the Title run.")
            return
        _compat(output, result)
        visual = title_flow.image_plan(result)
        # The only semantically authoritative CTA grouping is written by T2.
        visual["ebook_cta_groups"] = _cta_groups(result["blocks"])
        visual["ebook_reference"] = {
            "file": "ebook/cover_1.png", "sha256": title_flow.sha(ebook),
        }
        if args.stop == "T2":
            title_flow.write(output / "visual_plan.json", visual)
            _report(output, metadata, "stopped_T2", started)
            print("T2 completed. B2 merged output and visual plan exported.")
            return
        # Casero and Profesional retain their persisted visual style and voice.
        # Title uses a separate Gemini voice; noninteractive use requires --voice-id.
        existing_voice = avatar.parent / "meta" / "gemini_voice.json"
        if (not args.no_audio and not existing_voice.is_file()
                and not sys.stdin.isatty() and args.voice_id is None):
            raise SystemExit(
                "Titulo needs --voice-id for the first noninteractive Gemini setup"
            )
        if args.no_audio:
            voice_snapshot = None
        else:
            voice = ensure_professional_avatar_voice(
                avatar_file=avatar, input_voice_id=args.voice_id,
                gemini_api_key=settings.gemini_api_key,
            )
            voice_snapshot = voice.snapshot()
            _snapshot(
                output, "avatar_voice.json",
                (json.dumps(voice_snapshot, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        qr = ensure_avatar_qr(
            avatar_file=avatar, avatars_dir=args.data_root, input_url=args.qr_url,
        )
        _snapshot(output, "qr.png", qr.png)
        metadata["avatar"] = _avatar_meta(avatar, avatar_bytes, voice_snapshot or {}, qr)
        visual["avatar"] = metadata["avatar"]
        visual = _reconcile_visual_plan(output, visual)
        from ai_video_factory.workflows.r2_run_scope import (
            reset_active_video_scope,
            saved_video_scope,
            set_active_video_scope,
            video_run_scope,
        )
        scope = (
            saved_video_scope(output) if (output / "video").exists()
            else video_run_scope(output, output / "video" / "runs" / "__new__")
        )
        token = set_active_video_scope(scope)
        try:
            provider = OpenAIProvider(
                api_key=settings.openai_api_key,
                reasoning_effort=settings.openai_reasoning_effort,
                service_tier="default",
            )
            audio_file = output / "audio" / "latest.json"
            if args.no_audio:
                metadata["audio_generation"] = {"status": "skipped"}
            elif audio_file.is_file():
                saved_audio = title_flow.read(audio_file)
                if saved_audio.get("status") != "completed":
                    raise SystemExit("Incomplete Gemini audio: reconcile paid task before retry")
                metadata["audio_generation"] = saved_audio
            else:
                metadata["audio_generation"] = {"status": "running"}
                _report(output, metadata, "running", started)
                metadata["audio_generation"] = await generate_professional_audio(
                    core=result["narrative_core"], blocks=result["blocks"],
                    output=output, provider=provider, model=settings.openai_model,
                    gemini_api_key=settings.gemini_api_key,
                    voice_id=voice_snapshot["voice_id"],
                    source_sha256=metadata["script_sha256"],
                    max_parallel_calls=args.max_parallel_calls,
                )
            _report(output, metadata, "running", started)
            if not args.no_audio:
                await professional._preflight_saved_run_audio(output, visual, metadata)
                _report(output, metadata, "running", started)
            if args.no_image:
                metadata["image_generation"] = {"status": "skipped"}
            else:
                manifest_path = output / "images" / "manifest.json"
                if manifest_path.is_file():
                    saved_manifest = title_flow.read(manifest_path)
                    if (saved_manifest.get("status") == "completed" and
                            saved_manifest.get("finishing_status") == "completed"):
                        images = saved_manifest
                    else:
                        images = await professional._generate_images(output, visual)
                else:
                    images = await professional._generate_images(output, visual)
                metadata["image_generation"] = professional._image_summary(images)
                _report(output, metadata, "running", started)
                preview = await professional._preview_if_ready(
                    output, {"run": metadata}, plan=visual, images=images,
                )
                if preview:
                    metadata["video_preview"] = preview
                    _report(output, metadata, "running", started)
                if args.no_video:
                    metadata["video_generation"] = {"status": "skipped"}
                else:
                    metadata["video_generation"] = {"status": "running"}
                    _report(output, metadata, "running", started)
                    video = await professional._video_if_ready(
                        output, {"run": metadata}, plan=visual, images=images,
                    )
                    metadata["video_generation"] = video or {"status": "deferred"}
        finally:
            reset_active_video_scope(token)
        _report(output, metadata, "completed", started)
        if metadata.get("video_generation", {}).get("status") == "completed":
            await asyncio.to_thread(
                professional._publish_if_ready,
                output, script, args.results_root, args.managed_target,
            )
    except Exception:
        _report(output, metadata, "failed", started)
        raise


if __name__ == "__main__":
    asyncio.run(main())
