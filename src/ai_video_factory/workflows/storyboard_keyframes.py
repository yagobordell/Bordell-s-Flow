import asyncio
from pathlib import Path

from ai_video_factory.domain import ReferenceAsset, Shot, StoryboardFrame, StoryboardKeyframe
from ai_video_factory.providers.images import (
    ImageInputFidelity,
    ImageQuality,
    ImageReferenceInput,
    ReferenceAwareImageProvider,
)

_REFERENCE_GUIDANCE = """\
Create one new storyboard keyframe from the storyboard prompt below.
Use the supplied images only as canonical identity and appearance references for the entities.
Preserve recognizable historical design and appearance, but create a new composition for this shot.
Do not copy the reference images' neutral backgrounds, poses, framing, camera, or layout.

STORYBOARD PROMPT:
"""

_MEDIA_TYPE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


async def generate_storyboard_keyframes(
    frames: list[StoryboardFrame],
    shots: list[Shot],
    reference_assets: list[ReferenceAsset],
    *,
    image_provider: ReferenceAwareImageProvider,
    reference_root: Path,
    output_dir: Path,
    model: str,
    size: str,
    quality: ImageQuality,
    input_fidelity: ImageInputFidelity = "high",
) -> list[StoryboardKeyframe]:
    """Generate one reference-conditioned still image for every storyboard frame."""

    _validate_inputs(frames, shots, reference_assets)
    if not frames:
        return []

    assets_by_id = {asset.entity_id: asset for asset in reference_assets}
    references_by_shot = [
        _load_shot_references(
            shot,
            assets_by_id=assets_by_id,
            reference_root=reference_root,
        )
        for shot in shots
    ]

    generated_images = await asyncio.gather(
        *(
            _generate_keyframe(
                frame,
                references,
                image_provider=image_provider,
                model=model,
                size=size,
                quality=quality,
                input_fidelity=input_fidelity,
            )
            for frame, references in zip(frames, references_by_shot, strict=True)
        )
    )

    if any(image.extension != "png" for image in generated_images):
        raise ValueError("Storyboard keyframe workflow requires PNG provider output")
    if any(not image.content for image in generated_images):
        raise ValueError("Storyboard keyframe provider returned an empty image payload")

    output_dir.mkdir(parents=True, exist_ok=True)
    keyframes: list[StoryboardKeyframe] = []

    for frame, image in zip(frames, generated_images, strict=True):
        path = output_dir / f"shot_{frame.shot_id:03d}.png"
        path.write_bytes(image.content)
        keyframes.append(
            StoryboardKeyframe(
                shot_id=frame.shot_id,
                uri=path.relative_to(output_dir.parent).as_posix(),
            )
        )

    return keyframes


async def _generate_keyframe(
    frame: StoryboardFrame,
    references: list[ImageReferenceInput],
    *,
    image_provider: ReferenceAwareImageProvider,
    model: str,
    size: str,
    quality: ImageQuality,
    input_fidelity: ImageInputFidelity,
):
    if references:
        return await image_provider.generate_image_with_references(
            prompt=f"{_REFERENCE_GUIDANCE}{frame.prompt}",
            references=references,
            model=model,
            size=size,
            quality=quality,
            output_format="png",
            input_fidelity=input_fidelity,
        )

    return await image_provider.generate_image(
        prompt=frame.prompt,
        model=model,
        size=size,
        quality=quality,
        output_format="png",
    )


def _load_shot_references(
    shot: Shot,
    *,
    assets_by_id: dict[str, ReferenceAsset],
    reference_root: Path,
) -> list[ImageReferenceInput]:
    references: list[ImageReferenceInput] = []

    for entity_id in shot.entity_ids:
        asset = assets_by_id.get(entity_id)
        if asset is None:
            raise ValueError(
                f"Storyboard shot {shot.id} has no reference asset for entity {entity_id}"
            )

        path = _resolve_reference_path(reference_root, asset.uri)
        if not path.is_file():
            raise ValueError(f"Storyboard reference asset file not found: {path}")

        media_type = _MEDIA_TYPE_BY_SUFFIX.get(path.suffix.lower())
        if media_type is None:
            raise ValueError(f"Unsupported storyboard reference image format: {path.suffix}")

        content = path.read_bytes()
        if not content:
            raise ValueError(f"Storyboard reference asset is empty: {path}")
        references.append(ImageReferenceInput(content=content, media_type=media_type))

    return references


def _resolve_reference_path(reference_root: Path, uri: str) -> Path:
    root = reference_root.resolve()
    path = (reference_root / uri).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Reference asset URI escapes its root directory: {uri}")
    return path


def _validate_inputs(
    frames: list[StoryboardFrame],
    shots: list[Shot],
    reference_assets: list[ReferenceAsset],
) -> None:
    frame_ids = [frame.shot_id for frame in frames]
    shot_ids = [shot.id for shot in shots]
    if frame_ids != shot_ids:
        raise ValueError("Storyboard frame IDs must match shot IDs exactly and preserve order")
    if len(frame_ids) != len(set(frame_ids)):
        raise ValueError("Storyboard frame shot IDs must be unique")

    asset_ids = [asset.entity_id for asset in reference_assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("Storyboard reference assets must have unique entity IDs")

    for shot in shots:
        if len(shot.entity_ids) != len(set(shot.entity_ids)):
            raise ValueError(f"Storyboard shot {shot.id} contains duplicate entity IDs")
