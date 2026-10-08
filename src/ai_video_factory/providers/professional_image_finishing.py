"""Recoverable professional B2 postprocessing; never upscale video frames.

Only original per-beat PNGs are replaced, once finishing is verified. Full
paid grids remain immutable. Recovery JSON and transient paid edit inputs are
not user-facing image outputs; uncertain paid outcomes are never retried.
"""

from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from ai_video_factory.image_upscaling import ImageUpscaler
from ai_video_factory.providers.openai_batch_images import described_beats
from ai_video_factory.providers.openai_images import OpenAIImageClient
from ai_video_factory.providers.optium_image_upscaler import OptiumLabsImageUpscaler
from ai_video_factory.providers.professional_image_state import finishing_state_path

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
    return item["block_id"], item.get("asset_id", item["beat_id"])


def _record(
    item: dict[str, Any], *, file: str, content: bytes,
    output_size: tuple[int, int], recipe: str,
) -> dict[str, Any]:
    return {
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
    allow_ambiguous_upscale_resume: bool = False,
) -> dict[str, Any]:
    block, beat = _asset_key(item)
    png = output / item["file"]
    state = finishing_state_path(output, block, beat, recipe)
    folder = state.parent
    staged = png.with_name(png.name + ".finishing.part")
    if any(p.is_symlink() for p in (
        output / "images", png.parent, png, folder, state, staged,
    )):
        raise ProfessionalImageFinishingError("Linked professional finishing output")
    identity = {
        "profile": _PROFILE, "recipe": recipe, "source_sha256": item["sha256"],
        "target": target, "model": None,
        "avatar_reference_sha256": None,
        "prompt_sha256": fingerprint,
    }
    identity_hash = _digest(json.dumps(identity, sort_keys=True).encode("utf-8"))

    def clean_migrated_legacy(saved: dict[str, Any]) -> None:
        legacy_folder = output / "images" / "professional_finished" / f"block_{block}"
        old_state = legacy_folder / f"{beat}-{recipe}.json"
        old_png = old_state.with_suffix(".png")
        old_raw = legacy_folder / f"{beat}-{recipe}.native.png"
        if not old_state.exists() and not old_state.is_symlink():
            return
        if any(path.is_symlink() for path in (
            legacy_folder, old_state, old_png, old_raw,
        )):
            raise ProfessionalImageFinishingError("Linked migrated legacy output")
        previous = json.loads(old_state.read_text(encoding="utf-8"))
        if (previous.get("schema_version") != "professional-image-finishing-v1"
            or previous.get("identity") != identity_hash
            or previous.get("source") != item["file"]
            or previous.get("status") != "completed"
            or previous.get("sha256") != saved["sha256"]
            or not old_png.is_file()
            or _digest(old_png.read_bytes()) != saved["sha256"]):
            raise ProfessionalImageFinishingError("Unreconciled legacy finishing copy")
        old_png.unlink()
        old_state.unlink()
        if old_raw.is_file():
            old_raw.unlink()
        if not any(legacy_folder.iterdir()):
            legacy_folder.rmdir()
        if not any(legacy_folder.parent.iterdir()):
            legacy_folder.parent.rmdir()

    def completed_record(saved: dict[str, Any]) -> dict[str, Any]:
        if not png.is_file() or png.is_symlink():
            raise ProfessionalImageFinishingError("Completed finished PNG is missing")
        finished = png.read_bytes()
        if _digest(finished) != saved.get("sha256"):
            raise ProfessionalImageFinishingError("Finished PNG SHA mismatch")
        _check_png(finished, target)
        clean_migrated_legacy(saved)
        record = _record(
            item, file=item["file"], content=finished,
            output_size=target, recipe=recipe,
        )
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
                if staged.exists():
                    if _digest(staged.read_bytes()) != saved["sha256"]:
                        raise ProfessionalImageFinishingError("Unreconciled staged finished PNG")
                    staged.unlink()
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
            return completed_record(saved)
        if saved.get("status") != "pending":
            # Only an explicit --images-only recovery may resume an ambiguous
            # Salad x2 request. The real provider checks the deterministic R2
            # output first and, if still missing, reuses the exact same GPU
            # request identity. Never retry ambiguous paid GPT-image POSTs.
            if not (
                saved.get("status") == "upscale_submitting_unknown"
                and recipe == "image_x2"
                and allow_ambiguous_upscale_resume
                and isinstance(upscaler, OptiumLabsImageUpscaler)
            ):
                raise ProfessionalImageFinishingError(
                    f"Unknown outcome for {beat} ({recipe}); reconcile before any resubmission"
                )
    else:
        legacy_folder = output / "images" / "professional_finished" / f"block_{block}"
        legacy_state = legacy_folder / f"{beat}-{recipe}.json"
        legacy_png = legacy_state.with_suffix(".png")
        legacy_raw = legacy_folder / f"{beat}-{recipe}.native.png"
        if legacy_state.exists() or legacy_state.is_symlink():
            if any(path.is_symlink() for path in (
                legacy_folder, legacy_state, legacy_png, legacy_raw,
            )):
                raise ProfessionalImageFinishingError("Linked legacy professional output")
            old = json.loads(legacy_state.read_text(encoding="utf-8"))
            if (old.get("schema_version") != "professional-image-finishing-v1"
                or old.get("source") != item["file"]
                or old.get("identity") != identity_hash
                or old.get("status") != "completed"):
                raise ProfessionalImageFinishingError(
                    f"Legacy paid finishing state for {beat} requires reconciliation"
                )
            if not legacy_png.is_file():
                raise ProfessionalImageFinishingError("Legacy finished PNG is missing")
            legacy_bytes = legacy_png.read_bytes()
            if _digest(legacy_bytes) != old.get("sha256"):
                raise ProfessionalImageFinishingError("Legacy finished PNG SHA mismatch")
            _check_png(legacy_bytes, target)
            _source(output, item)  # Keep the paid original identity verifiable.
            saved = {
                "schema_version": "professional-image-finishing-v1",
                "identity": identity_hash, "status": "ready_to_replace",
                "source": item["file"], "source_sha256": item["sha256"],
                "block_id": block, "beat_id": beat,
                "model_id": None,
                "artifact": old.get("artifact", {}),
                "sha256": old["sha256"], "output_file": item["file"],
            }
            if staged.exists() or staged.is_symlink():
                if (staged.is_symlink()
                    or _digest(staged.read_bytes()) != old["sha256"]):
                    raise ProfessionalImageFinishingError("Unreconciled staged legacy copy")
            else:
                staged.write_bytes(legacy_bytes)
            # Persist only ready-to-replace; a crash before this checkpoint
            # leaves the legacy paid result intact and never resubmits it.
            _save_json(state, saved)
            if _digest(png.read_bytes()) != item["sha256"]:
                raise ProfessionalImageFinishingError("Legacy original changed during migration")
            staged.replace(png)
            saved["status"] = "completed"
            _save_json(state, saved)
            # Only delete the old copy once its new destination and durable
            # recovery state are confirmed. Never retain duplicate final PNGs.
            legacy_png.unlink()
            legacy_state.unlink()
            if legacy_raw.is_file():
                legacy_raw.unlink()
            if not any(legacy_folder.iterdir()):
                legacy_folder.rmdir()
            if not any(legacy_folder.parent.iterdir()):
                legacy_folder.parent.rmdir()
            return completed_record(saved)
        if legacy_png.exists() or legacy_png.is_symlink():
            raise ProfessionalImageFinishingError("Untracked legacy finished PNG")
        saved = {
            "schema_version": "professional-image-finishing-v1",
            "identity": identity_hash, "status": "pending",
            "source": item["file"], "source_sha256": item["sha256"],
            "block_id": block, "beat_id": beat,
            "model_id": None,
        }
        _save_json(state, saved)

    # The source exists only until its own finishing succeeds. A resumed,
    # already finished beat returns above without requiring the old bytes.
    source = _source(output, item)
    if staged.exists() or staged.is_symlink():
        raise ProfessionalImageFinishingError("Unreconciled partial finished PNG")
    if recipe == "native_video_crop":
        finished = _video_crop(source)
    elif recipe == "image_x2":
        saved["status"] = "upscale_submitting_unknown"
        _save_json(state, saved)
        if upscaler is None:
            raise ProfessionalImageFinishingError("OptiumLabs image upscaler is not configured")
        finished = upscaler.upscale(
            source, target_width=target[0], target_height=target[1],
        )
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
    return completed_record(saved)


def finish_professional_images(
    output: Path, plan: dict[str, Any], manifest: dict[str, Any],
    *, upscaler: ImageUpscaler | None,
    api_key: str | None, direct_client: OpenAIImageClient | None = None,
    allow_ambiguous_upscale_resume: bool = False,
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
        (item["block_id"], item.get("asset_id", item["beat_id"]))
        for item in described_beats(plan)
    }
    if {_asset_key(item) for item in originals} != expected or len(originals) != len(expected):
        raise ProfessionalImageFinishingError("Professional original image assets are incomplete")
    # Static images still require exact x2 processing. Grid video panels do
    # not require credentials: they are already paid images.
    if any(item["visual_type"] in {"media_image", "avatar_media"} for item in originals):
        if upscaler is None:
            raise ProfessionalImageFinishingError("IMAGE_UPSCALER_URL and Salad/R2 required")
    final: list[dict[str, Any]] = []
    manifest_path = output / "images" / "manifest.json"
    manifest["finishing_profile"] = _PROFILE
    # Preserve historical accounting for older saved runs, but never book a
    # fresh paid per-panel edit.
    manifest["native_grid_video_calls"] = manifest.get("native_grid_video_calls", 0)
    manifest["finishing_status"] = "running"
    manifest["final_assets"] = []
    _save_json(manifest_path, manifest)
    try:
        for item in originals:
            if item["visual_type"] in {"media_image", "avatar_media"}:
                width, height = item["width"], item["height"]
                result = _materialize(
                    output, item, recipe="image_x2",
                    target=(width * 2, height * 2), fingerprint="exact-image-x2",
                    upscaler=upscaler,
                    allow_ambiguous_upscale_resume=allow_ambiguous_upscale_resume,
                )
            elif item["visual_type"] in {"media_video", "avatar"}:
                if item.get("provider") == "grid_crop":
                    # A grid crop is the FINAL source for video. Do not upscale
                    # it or create a second paid GPT image from it.
                    prior_state = finishing_state_path(
                        output, item["block_id"],
                        item.get("asset_id", item["beat_id"]), "native_grid_video"
                    )
                    if prior_state.exists() or prior_state.is_symlink():
                        # Do not silently discard a previously paid edit or an
                        # unknown POST from an older pipeline version.
                        raise ProfessionalImageFinishingError(
                            f"Historical native grid-video finishing state for "
                            f"{item['beat_id']} requires explicit reconciliation"
                        )
                    dimensions = item["width"], item["height"]
                    if dimensions not in {_VIDEO_TARGET, (1280, 720)}:
                        raise ProfessionalImageFinishingError(
                            f"Unsupported grid-video panel geometry for {item['beat_id']}"
                        )
                    content = _source(output, item)
                    _check_png(content, dimensions)
                    result = _record(
                        item, file=item["file"], content=content,
                        output_size=dimensions, recipe="grid_video_crop",
                    )
                else:
                    if (item.get("width"), item.get("height")) != _VIDEO_SOURCE:
                        raise ProfessionalImageFinishingError(
                            "Professional standalone video GPT source is not native 1920x1088"
                        )
                    result = _materialize(
                        output, item, recipe="native_video_crop",
                        target=_VIDEO_TARGET, fingerprint="center-crop-8px",
                        upscaler=None,
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
