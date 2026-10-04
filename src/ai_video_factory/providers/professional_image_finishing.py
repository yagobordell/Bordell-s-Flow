"""Recoverable professional B2 postprocessing; never upscale video frames.

Only original per-beat PNGs are replaced, once finishing is verified. Full
paid grids remain immutable. Recovery JSON and transient paid edit inputs are
not user-facing image outputs; uncertain paid outcomes are never retried.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.image_upscaling import ImageUpscaler
from ai_video_factory.providers.professional_image_state import finishing_state_path
from ai_video_factory.providers.openai_images import OpenAIImageClient

_VIDEO_SOURCE = (1920, 1088)
_VIDEO_TARGET = (1920, 1080)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PROFILE = "professional-image-x2-video-native1080-v1"


class ProfessionalImageFinishingError(RuntimeError):
    """Keep every original paid PNG and failed finishing state for recovery."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ProfessionalImageFinishingError("Linked professional image state")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".tmp")
    if part.is_symlink():
        raise ProfessionalImageFinishingError("Linked professional image temporary state")
    part.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    part.replace(path)


def _source(output: Path, item: dict[str, Any]) -> bytes:
    name, digest = item.get("file"), item.get("sha256")
    if not isinstance(name, str) or not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ProfessionalImageFinishingError("Invalid source image identity")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "images":
        raise ProfessionalImageFinishingError("Source image is outside images")
    path = output / relative
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ProfessionalImageFinishingError(f"Source PNG missing or linked: {name}")
    content = path.read_bytes()
    if _digest(content) != digest:
        raise ProfessionalImageFinishingError(f"Source PNG SHA mismatch: {name}")
    with Image.open(BytesIO(content)) as image:
        if image.format != "PNG":
            raise ProfessionalImageFinishingError("Source image must be PNG")
        image.verify()
    return content


def _check_png(content: bytes, size: tuple[int, int]) -> None:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or image.size != size:
                raise ProfessionalImageFinishingError(
                    f"Finished image must be PNG {size[0]}x{size[1]}"
                )
            image.verify()
    except (OSError, ValueError) as exc:
        raise ProfessionalImageFinishingError("Unreadable finished PNG") from exc


def _video_crop(data: bytes) -> bytes:
    _check_png(data, _VIDEO_SOURCE)
    with Image.open(BytesIO(data)) as image:
        finished = image.crop((0, 4, _VIDEO_TARGET[0], 1084))
        result = BytesIO()
        finished.save(result, format="PNG")
    return result.getvalue()


def _asset_key(item: dict[str, Any]) -> tuple[int, str]:
    return item["block_id"], item["beat_id"]


def _record(
    item: dict[str, Any], *, file: str, content: bytes,
    output_size: tuple[int, int], recipe: str,
) -> dict[str, Any]:
    return {
        "block_id": item["block_id"],
        "beat_id": item["beat_id"],
        "visual_type": item["visual_type"],
        "file": file,
        "sha256": _digest(content),
        "width": output_size[0],
        "height": output_size[1],
        "provider": recipe,
        "source_file": item["file"],
        "source_sha256": item["sha256"],
        "finishing_profile": _PROFILE,
    }


def _materialize(
    output: Path, item: dict[str, Any], *, recipe: str,
    target: tuple[int, int], fingerprint: str,
    upscaler: ImageUpscaler | None,
    direct: OpenAIImageClient | None, model: str,
    avatar_reference: bytes | None = None,
) -> dict[str, Any]:
    block, beat = _asset_key(item)
    png = output / item["file"]
    state = finishing_state_path(output, block, beat, recipe)
    folder = state.parent
    staged = png.with_name(png.name + ".finishing.part")
    raw = folder / f"{beat}-{recipe}.native.png"
    if any(p.is_symlink() for p in (
        output / "images", png.parent, png, folder, state, staged, raw,
    )):
        raise ProfessionalImageFinishingError("Linked professional finishing output")
    identity = {
        "profile": _PROFILE, "recipe": recipe, "source_sha256": item["sha256"],
        "target": target, "model": model if recipe == "native_grid_video" else None,
        "avatar_reference_sha256": _digest(avatar_reference) if avatar_reference else None,
        "prompt_sha256": fingerprint,
    }
    identity_hash = _digest(json.dumps(identity, sort_keys=True).encode("utf-8"))

    def completed_record(saved: dict[str, Any]) -> dict[str, Any]:
        if not png.is_file() or png.is_symlink():
            raise ProfessionalImageFinishingError("Completed finished PNG is missing")
        finished = png.read_bytes()
        if _digest(finished) != saved.get("sha256"):
            raise ProfessionalImageFinishingError("Finished PNG SHA mismatch")
        _check_png(finished, target)
        record = _record(
            item, file=item["file"], content=finished,
            output_size=target, recipe=recipe,
        )
        if recipe == "native_grid_video":
            record["openai_usage"] = saved.get("artifact", {}).get("openai_usage")
        return record

    if state.is_file():
        saved = json.loads(state.read_text(encoding="utf-8"))
        if saved.get("identity") != identity_hash:
            raise ProfessionalImageFinishingError(
                f"Changed professional image finishing recipe for {beat}; reconcile first"
            )
        if saved.get("source_sha256") != item["sha256"] or (
            saved.get("output_file") not in (None, item["file"])
        ):
            raise ProfessionalImageFinishingError("Invalid saved professional finishing source")
        if saved.get("status") == "completed":
            if saved.get("output_file") != item["file"]:
                raise ProfessionalImageFinishingError("Missing finished image destination")
            return completed_record(saved)
        if saved.get("status") == "ready_to_replace":
            # Resolve either side of an interrupted atomic replacement.
            if png.is_file() and _digest(png.read_bytes()) == saved.get("sha256"):
                pass
            elif (
                png.is_file() and _digest(png.read_bytes()) == item["sha256"]
                and staged.is_file() and _digest(staged.read_bytes()) == saved.get("sha256")
            ):
                _check_png(staged.read_bytes(), target)
                staged.replace(png)
            else:
                raise ProfessionalImageFinishingError(
                    f"Unreconciled in-place image replacement for {beat}"
                )
            saved["status"] = "completed"
            _save_json(state, saved)
            if raw.exists():
                raw.unlink()
            return completed_record(saved)
        if saved.get("status") != "pending":
            raise ProfessionalImageFinishingError(
                f"Unknown outcome for {beat} ({recipe}); reconcile before any resubmission"
            )
    else:
        saved = {
            "schema_version": "professional-image-finishing-v1",
            "identity": identity_hash, "status": "pending",
            "source": item["file"], "source_sha256": item["sha256"],
            "block_id": block, "beat_id": beat,
            "model_id": model if recipe == "native_grid_video" else None,
        }
        _save_json(state, saved)

    # The source exists only until its own finishing succeeds. A resumed,
    # already finished beat returns above without requiring the old bytes.
    source = _source(output, item)
    if staged.exists() or staged.is_symlink():
        raise ProfessionalImageFinishingError("Unreconciled partial finished PNG")
    if recipe == "native_video_crop":
        finished = _video_crop(source)
    else:
        saved["status"] = (
            "direct_submitting_unknown" if recipe == "native_grid_video"
            else "upscale_submitting_unknown"
        )
        _save_json(state, saved)
        if recipe == "image_x2":
            if upscaler is None:
                raise ProfessionalImageFinishingError("OptiumLabs image upscaler is not configured")
            finished = upscaler.upscale(
                source, target_width=target[0], target_height=target[1],
            )
        elif recipe == "native_grid_video":
            if direct is None:
                raise ProfessionalImageFinishingError("GPT Image client is unavailable")
            refs = ["data:image/png;base64," + base64.b64encode(source).decode("ascii")]
            if avatar_reference is not None:
                refs.append(
                    "data:image/png;base64," + base64.b64encode(avatar_reference).decode("ascii")
                )
            description = re.sub(r"^[1-9]\.\s+", "", item.get("description", "")).strip()
            text = (
                "Render the first reference scene as an authentic high-end iPhone 16/17 "
                "Pro Max photograph in instagram rhode style at native high resolution. "
                "Preserve its exact subject identities, viewpoint, composition, scene "
                "relationships, lighting, color palette and continuity with the other "
                "grid panels. Preserve the scene detail: " + description + ". "
                "The first reference image defines the complete scene."
            )
            if avatar_reference is not None:
                text += " The second reference preserves the avatar's facial identity."
            folder.mkdir(parents=True, exist_ok=True)
            if raw.exists() or raw.is_symlink():
                raise ProfessionalImageFinishingError("Untracked native GPT source exists")
            generated = direct.generate_png(
                prompt=text, model=model, destination=raw,
                size="1920x1088", quality="medium", output_format="png",
                reference_image_urls=refs,
            )
            raw_data = raw.read_bytes()
            _check_png(raw_data, _VIDEO_SOURCE)
            finished = _video_crop(raw_data)
            saved["artifact"] = {
                "model_id": model, "quality": "medium",
                "openai_usage": generated.get("usage"),
                "openai_created": generated.get("created"),
            }
        else:
            raise ProfessionalImageFinishingError(f"Unknown finishing recipe: {recipe}")
    _check_png(finished, target)
    # Stage and record the digest BEFORE swapping out the original PNG.
    staged.write_bytes(finished)
    saved.update({
        "status": "ready_to_replace", "sha256": _digest(finished),
        "output_file": item["file"],
    })
    _save_json(state, saved)
    if _digest(png.read_bytes()) != item["sha256"]:
        raise ProfessionalImageFinishingError("Source changed before atomic replacement")
    staged.replace(png)
    saved["status"] = "completed"
    _save_json(state, saved)
    if raw.exists():
        raw.unlink()
    return completed_record(saved)


def finish_professional_images(
    output: Path, plan: dict[str, Any], manifest: dict[str, Any],
    *, upscaler: ImageUpscaler | None,
    api_key: str | None, direct_client: OpenAIImageClient | None = None,
) -> list[dict[str, Any]]:
    """Replace each beat PNG only after verified and recoverable finishing.

    Full paid grids and durable paid request metadata remain untouched.
    """
    if plan.get("visual_style") != "profesional" or manifest.get("status") != "completed":
        raise ProfessionalImageFinishingError("Requires completed professional GPT image inputs")
    originals = [
        *(item for item in manifest.get("items", []) if item.get("grid") is None),
        *manifest.get("panel_assets", []),
    ]
    expected = {
        (block["block_id"], beat["beat_id"])
        for block in plan["blocks"] for beat in block["beats"]
        if beat.get("prompt") is not None
    }
    if {_asset_key(item) for item in originals} != expected or len(originals) != len(expected):
        raise ProfessionalImageFinishingError("Professional original image assets are incomplete")
    video_grid = any(
        item.get("provider") == "grid_crop"
        and item["visual_type"] in {"media_video", "avatar"}
        for item in originals
    )
    # Require all credentials BEFORE attempting any new remote work.
    if any(item["visual_type"] in {"media_image", "avatar_media"} for item in originals):
        if upscaler is None:
            raise ProfessionalImageFinishingError("IMAGE_UPSCALER_URL and Salad/R2 required")
    if video_grid and direct_client is None:
        if not api_key:
            raise ProfessionalImageFinishingError("OPENAI_API_KEY required for native grid video")
        direct_client = OpenAIImageClient(api_key)
    avatar_bytes: bytes | None = None
    final: list[dict[str, Any]] = []
    manifest_path = output / "images" / "manifest.json"
    manifest["finishing_profile"] = _PROFILE
    manifest["native_grid_video_calls"] = sum(
        item.get("provider") == "grid_crop"
        and item["visual_type"] in {"media_video", "avatar"}
        for item in originals
    )
    manifest["finishing_status"] = "running"
    manifest["final_assets"] = []
    _save_json(manifest_path, manifest)
    try:
        for item in originals:
            if item["visual_type"] in {"media_image", "avatar_media"}:
                with Image.open(BytesIO(source)) as image:
                    width, height = image.size
                result = _materialize(
                    output, item, recipe="image_x2",
                    target=(width * 2, height * 2), fingerprint="exact-image-x2",
                    upscaler=upscaler, direct=None, model="",
                )
            elif item["visual_type"] in {"media_video", "avatar"}:
                if item.get("provider") == "grid_crop":
                    if item.get("reference"):
                        if avatar_bytes is None:
                            meta = plan.get("avatar")
                            path = output / "avatar.png"
                            if (not isinstance(meta, dict) or path.is_symlink()
                                or not path.is_file() or _digest(path.read_bytes())
                                != meta.get("sha256")):
                                raise ProfessionalImageFinishingError(
                                    "Saved avatar reference is not verifiable"
                                )
                            avatar_bytes = path.read_bytes()
                    description = next(
                        (beat["prompt"] for block in plan["blocks"]
                         for beat in block["beats"]
                         if (block["block_id"], beat["beat_id"]) == _asset_key(item)), ""
                    )
                    # Include the original description and model in paid identity.
                    annotated = {**item, "description": description}
                    result = _materialize(
                        output, annotated, recipe="native_grid_video", target=_VIDEO_TARGET,
                        fingerprint=_digest(description.encode("utf-8")),
                        upscaler=None, direct=direct_client,
                        model=manifest["model_id"],
                        avatar_reference=avatar_bytes if item.get("reference") else None,
                    )
                else:
                    if (item.get("width"), item.get("height")) != _VIDEO_SOURCE:
                        raise ProfessionalImageFinishingError(
                            "Professional standalone video GPT source is not native 1920x1088"
                        )
                    result = _materialize(
                        output, item, recipe="native_video_crop",
                        target=_VIDEO_TARGET, fingerprint="center-crop-8px",
                        upscaler=None, direct=None, model="",
                    )
            else:
                raise ProfessionalImageFinishingError(
                    f"Unexpected professional image kind: {item['visual_type']}"
                )
            final.append(result)
            manifest["final_assets"] = final.copy()
            _save_json(manifest_path, manifest)
    except Exception:
        manifest["finishing_status"] = "incomplete"
        _save_json(manifest_path, manifest)
        raise
    manifest["finishing_status"] = "completed"
    _save_json(manifest_path, manifest)
    return final
