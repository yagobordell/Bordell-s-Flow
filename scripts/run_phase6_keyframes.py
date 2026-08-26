import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.config import settings
from ai_video_factory.domain import ReferenceAsset, Shot, StoryboardFrame
from ai_video_factory.providers.openai_images import OpenAIImageProvider
from ai_video_factory.workflows.storyboard_keyframes import generate_storyboard_keyframes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate reference-conditioned storyboard keyframe images for planned shots."
    )
    parser.add_argument(
        "--frames",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_frames.json",
    )
    parser.add_argument(
        "--shots",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
    )
    parser.add_argument(
        "--reference-assets",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets.json",
    )
    parser.add_argument(
        "--model",
        default=settings.openai_image_model,
        help="Image model. Defaults to OPENAI_IMAGE_MODEL.",
    )
    parser.add_argument(
        "--size",
        default="1024x1536",
        help="Generated keyframe size.",
    )
    parser.add_argument(
        "--quality",
        choices=("low", "medium", "high", "auto"),
        default="medium",
    )
    parser.add_argument(
        "--input-fidelity",
        choices=("low", "high"),
        default="high",
        help="How strongly image edits should preserve canonical reference appearance.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_keyframes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_keyframes.json",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")

    frames = _read_models(args.frames, StoryboardFrame)
    shots = _read_models(args.shots, Shot)
    reference_assets = _read_models(args.reference_assets, ReferenceAsset)

    image_provider = OpenAIImageProvider(api_key=settings.openai_api_key)
    keyframes = await generate_storyboard_keyframes(
        frames,
        shots,
        reference_assets,
        image_provider=image_provider,
        reference_root=args.reference_assets.parent,
        output_dir=args.output_dir,
        model=args.model,
        size=args.size,
        quality=args.quality,
        input_fidelity=args.input_fidelity,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([keyframe.model_dump() for keyframe in keyframes], indent=2),
        encoding="utf-8",
    )

    print(f"Phase 6 storyboard keyframes complete. Metadata: {args.output.resolve()}")
    print(f"Generated {len(keyframes)} keyframe PNG files in {args.output_dir.resolve()}")


def _read_models[ModelT: BaseModel](
    path: Path,
    model_type: type[ModelT],
) -> list[ModelT]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")

    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {path}")

    return [model_type.model_validate(item) for item in raw]


if __name__ == "__main__":
    asyncio.run(main())
