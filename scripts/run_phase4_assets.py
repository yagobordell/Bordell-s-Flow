import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.providers import OpenAIImageProvider
from ai_video_factory.workflows.reference_assets import generate_reference_assets

DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "medium"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate canonical reference images from Phase 4 visual reference prompts."
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
        help="Phase 4 visual_references.json file.",
    )
    parser.add_argument(
        "--model",
        default=settings.openai_image_model,
        help="Image generation model.",
    )
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        help="Generated image size, for example 1024x1024.",
    )
    parser.add_argument(
        "--quality",
        choices=("low", "medium", "high"),
        default=DEFAULT_QUALITY,
        help="Image generation quality.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets",
        help="Directory where generated PNG reference assets will be written.",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets.json",
        help="Path where ReferenceAsset metadata will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    if not args.references_file.is_file():
        raise SystemExit(f"Visual references file not found: {args.references_file}")

    raw: Any = json.loads(args.references_file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Visual references file must contain a JSON array.")

    references = [VisualReference.model_validate(item) for item in raw]
    provider = OpenAIImageProvider(api_key=settings.openai_api_key)
    assets = await generate_reference_assets(
        references,
        image_provider=provider,
        output_dir=args.output_dir,
        model=args.model,
        size=args.size,
        quality=args.quality,
    )

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    payload = [asset.model_dump() for asset in assets]
    args.metadata.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 4 reference assets complete. Metadata: {args.metadata.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
