"""Generate B3 narrator start images from the immediately preceding effective shot."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .openai_batch_images import ImageOptions, effective_image_prompt
from .openai_images import OpenAIImageClient, OpenAIImageRequestRejected

_SOURCE_SIZE = "1920x1088"
_TARGET_SIZE = (1920, 1080)


class B3ShotImageError(RuntimeError):
    """A B3 continuity start image could not be generated safely."""


def _sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise B3ShotImageError(f"Missing or linked B3 reference image: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink() or path.parent.is_symlink():
        raise B3ShotImageError(f"Refusing linked B3 shot state: {path}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _crop_video_source(source: Path, destination: Path) -> str:
    try:
        with Image.open(BytesIO(source.read_bytes())) as image:
            if image.format != "PNG" or image.size != (1920, 1088):
                raise B3ShotImageError("B3 source image must be a 1920x1088 PNG")
            cropped = image.crop((0, 4, 1920, 1084))
            output = BytesIO()
            cropped.save(output, format="PNG")
            data = output.getvalue()
    except (OSError, ValueError) as exc:
        raise B3ShotImageError("B3 source image is invalid") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    if destination.is_symlink() or temporary.is_symlink():
        raise B3ShotImageError("Refusing linked B3 shot output")
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def _asset_map(
    output: Path, plan: dict[str, Any],
) -> dict[tuple[int, str], Path]:
    assets: dict[tuple[int, str], Path] = {}
    for item in plan.get("image_assets", []):
        if not isinstance(item, dict):
            continue
        key = item.get("block_id"), item.get("beat_id")
        name, digest = item.get("file"), item.get("sha256")
        if (
            not isinstance(key[0], int)
            or not isinstance(key[1], str)
            or not isinstance(name, str)
            or not isinstance(digest, str)
        ):
            continue
        path = output / name
        if _sha(path) != digest:
            raise B3ShotImageError(f"B2 image asset changed for {key[1]}")
        assets[(key[0], key[1])] = path
    return assets


def _avatar_path(output: Path, plan: dict[str, Any]) -> Path:
    metadata = plan.get("avatar")
    if not isinstance(metadata, dict):
        raise B3ShotImageError("B3 requires the saved avatar metadata")
    name, digest = metadata.get("file"), metadata.get("sha256")
    if not isinstance(name, str) or not isinstance(digest, str):
        raise B3ShotImageError("B3 avatar metadata is invalid")
    path = output / name
    if _sha(path) != digest:
        raise B3ShotImageError("B3 avatar differs from the saved run")
    return path


def _b2_start_source(
    beat: dict[str, Any],
    *,
    block_id: int,
    avatar: Path,
    assets: dict[tuple[int, str], Path],
) -> Path:
    visual = beat.get("visual_type")
    if visual == "media_video" or (
        visual == "avatar" and beat.get("prompt") is not None
    ):
        key = block_id, beat["beat_id"]
        if key not in assets:
            raise B3ShotImageError(
                f"Missing B2 speaker start image for {beat['beat_id']}"
            )
        return assets[key]
    if visual in {"avatar", "avatar_media"}:
        return avatar
    raise B3ShotImageError(
        f"Unsupported audio=true B3 speaker source: {visual!r}"
    )


def _fingerprint(
    *, prompt: str, reference_sha256: str, options: ImageOptions,
) -> str:
    payload = {
        "prompt": prompt,
        "reference_sha256": reference_sha256,
        "model": options.model_id,
        "size": _SOURCE_SIZE,
        "quality": "low",
        "recipe": "b3-previous-shot-continuity-v1",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _generate_one(
    *,
    output: Path,
    block_id: int,
    beat: dict[str, Any],
    reference: Path,
    api_key: str | None,
    options: ImageOptions,
    direct_client: OpenAIImageClient | None,
) -> dict[str, Any]:
    prompt = beat.get("b3_shot_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise B3ShotImageError(f"B3 shot {beat['beat_id']} has no prompt")
    folder = output / "images" / "b3_shots"
    state_path = folder / f"{beat['beat_id']}.json"
    raw_path = folder / f"{beat['beat_id']}.source.png"
    final_path = folder / f"{beat['beat_id']}.png"
    if any(path.is_symlink() for path in (folder, state_path, raw_path, final_path)):
        raise B3ShotImageError("Refusing linked B3 shot paths")
    reference_sha = _sha(reference)
    fingerprint = _fingerprint(
        prompt=prompt, reference_sha256=reference_sha, options=options
    )
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            state.get("schema_version") != "b3-shot-image-v1"
            or state.get("fingerprint") != fingerprint
        ):
            raise B3ShotImageError(
                f"Saved B3 shot state differs for {beat['beat_id']}"
            )
    else:
        state = {
            "schema_version": "b3-shot-image-v1",
            "block_id": block_id,
            "beat_id": beat["beat_id"],
            "fingerprint": fingerprint,
            "reference_sha256": reference_sha,
            "status": "pending",
        }
        _atomic_json(state_path, state)
    if state["status"] == "completed":
        if final_path.is_file() and _sha(final_path) == state.get("sha256"):
            return state["artifact"]
        raise B3ShotImageError(
            f"Completed B3 shot {beat['beat_id']} is missing or changed"
        )
    if state["status"] == "submitting_unknown":
        raise B3ShotImageError(
            f"B3 shot {beat['beat_id']} has an unknown paid request outcome"
        )
    if state["status"] == "rejected":
        state["status"] = "pending"
        state.pop("error", None)
        state.pop("http_status", None)
        _atomic_json(state_path, state)
    if direct_client is None:
        if not api_key:
            raise B3ShotImageError("OPENAI_API_KEY is required for B3 shot generation")
        direct_client = OpenAIImageClient(api_key)
    state["status"] = "submitting_unknown"
    state["started_at_unix"] = time.time()
    _atomic_json(state_path, state)
    try:
        generated = direct_client.generate_png(
            prompt=(
                effective_image_prompt(prompt, visual_style="profesional")
                + "\n\nUse the attached previous narrator shot as the continuity "
                "reference. Preserve identity, narration setup, physical placement, "
                "wardrobe, lighting and persistent props while applying only the "
                "requested camera/framing change."
            ),
            model=options.model_id,
            size=_SOURCE_SIZE,
            quality="low",
            output_format="png",
            destination=raw_path,
            reference_image_url=_data_url(reference),
        )
    except OpenAIImageRequestRejected as exc:
        state["status"] = "rejected"
        state["http_status"] = exc.status_code
        state["error"] = exc.api_message
        _atomic_json(state_path, state)
        raise B3ShotImageError(
            f"B3 image POST for {beat['beat_id']} was rejected: {exc.api_message}"
        ) from exc
    except Exception as exc:
        raise B3ShotImageError(
            f"B3 image POST outcome for {beat['beat_id']} is unknown"
        ) from exc
    final_sha = _crop_video_source(raw_path, final_path)
    artifact = {
        "block_id": block_id,
        "beat_id": beat["beat_id"],
        "file": final_path.relative_to(output).as_posix(),
        "sha256": final_sha,
        "provider": "openai_direct",
        "model_id": options.model_id,
        "source_size": generated["size"],
        "width": _TARGET_SIZE[0],
        "height": _TARGET_SIZE[1],
        "reference_sha256": reference_sha,
        "status": "completed",
    }
    state["status"] = "completed"
    state["sha256"] = final_sha
    state["artifact"] = artifact
    _atomic_json(state_path, state)
    return artifact


def generate_b3_shot_images(
    output: Path,
    plan: dict[str, Any],
    *,
    api_key: str | None,
    options: ImageOptions,
    direct_client: OpenAIImageClient | None = None,
) -> list[dict[str, Any]]:
    """Generate only B3-authored new-take images, sequentially by spoken run."""
    if plan.get("visual_style") != "profesional":
        return []
    avatar = _avatar_path(output, plan)
    assets = _asset_map(output, plan)
    generated: list[dict[str, Any]] = []
    active: Path | None = None
    in_audio_run = False
    for block in plan.get("blocks", []):
        block_id = block["block_id"]
        for beat in block["beats"]:
            if beat.get("audio") is not True:
                active = None
                in_audio_run = False
                continue
            if not in_audio_run:
                active = None
                in_audio_run = True
            continuous = beat.get("continuous")
            if not isinstance(continuous, bool):
                raise B3ShotImageError(
                    f"Professional audio beat {beat['beat_id']} is missing B3 continuous"
                )
            if continuous:
                if active is None:
                    raise B3ShotImageError(
                        f"B3 beat {beat['beat_id']} cannot continue without an active shot"
                    )
                continue
            prompt = beat.get("b3_shot_prompt")
            if prompt is not None:
                reference = active if active is not None else avatar
                artifact = _generate_one(
                    output=output,
                    block_id=block_id,
                    beat=beat,
                    reference=reference,
                    api_key=api_key,
                    options=options,
                    direct_client=direct_client,
                )
                generated.append(artifact)
                active = output / artifact["file"]
            else:
                active = _b2_start_source(
                    beat, block_id=block_id, avatar=avatar, assets=assets
                )
    return generated
