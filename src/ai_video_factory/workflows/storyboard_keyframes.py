import asyncio
from pathlib import Path

from ai_video_factory.domain import Shot, StoryboardFrame, StoryboardKeyframe
from ai_video_factory.providers.images import (
    ImageProvider,
    ImageQuality,
    require_generated_image_geometry,
)


async def generate_storyboard_keyframes(
    frames: list[StoryboardFrame],
    shots: list[Shot],
    *,
    image_provider: ImageProvider,
    output_dir: Path,
    model: str,
    size: str,
    quality: ImageQuality,
) -> list[StoryboardKeyframe]:
    """Generate one text-conditioned still image for every storyboard frame."""

    _validate_inputs(frames, shots)
    if not frames:
        return []

    generated_images = await asyncio.gather(
        *(
            image_provider.generate_image(
                prompt=frame.prompt,
                model=model,
                size=size,
                quality=quality,
                output_format="png",
            )
            for frame in frames
        )
    )

    if any(image.extension != "png" for image in generated_images):
        raise ValueError("Storyboard keyframe workflow requires PNG provider output")
    if any(not image.content for image in generated_images):
        raise ValueError("Storyboard keyframe provider returned an empty image payload")
    for frame, image in zip(frames, generated_images, strict=True):
        require_generated_image_geometry(
            image,
            size=size,
            label=f"Storyboard keyframe {frame.shot_id}",
        )

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


def _validate_inputs(
    frames: list[StoryboardFrame],
    shots: list[Shot],
) -> None:
    frame_ids = [frame.shot_id for frame in frames]
    shot_ids = [shot.id for shot in shots]
    if frame_ids != shot_ids:
        raise ValueError("Storyboard frame IDs must match shot IDs exactly and preserve order")
    if len(frame_ids) != len(set(frame_ids)):
        raise ValueError("Storyboard frame shot IDs must be unique")

    for shot in shots:
        if len(shot.entity_ids) != len(set(shot.entity_ids)):
            raise ValueError(f"Storyboard shot {shot.id} contains duplicate entity IDs")
