import asyncio
from pathlib import Path

from ai_video_factory.domain import ReferenceAsset, VisualReference
from ai_video_factory.providers.images import GeneratedImage, ImageProvider, ImageQuality


async def generate_reference_assets(
    references: list[VisualReference],
    *,
    image_provider: ImageProvider,
    output_dir: Path,
    model: str,
    size: str,
    quality: ImageQuality,
) -> list[ReferenceAsset]:
    """Generate reference images concurrently and persist them with deterministic names."""

    entity_ids = [reference.entity_id for reference in references]
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError("Visual references must have unique entity IDs")

    if not references:
        return []

    results = await asyncio.gather(
        *(
            image_provider.generate_image(
                prompt=reference.prompt,
                model=model,
                size=size,
                quality=quality,
                output_format="png",
            )
            for reference in references
        ),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]
    generated_images = [
        result for result in results if isinstance(result, GeneratedImage)
    ]
    if len(generated_images) != len(references):
        raise RuntimeError("Reference image provider returned an unexpected result type")

    if any(image.extension != "png" for image in generated_images):
        raise ValueError("Reference asset workflow requires PNG provider output")

    output_dir.mkdir(parents=True, exist_ok=True)
    assets: list[ReferenceAsset] = []

    for reference, image in zip(references, generated_images, strict=True):
        path = output_dir / f"{reference.entity_id}.png"
        path.write_bytes(image.content)
        assets.append(
            ReferenceAsset(
                entity_id=reference.entity_id,
                uri=path.relative_to(output_dir.parent).as_posix(),
            )
        )

    return assets
