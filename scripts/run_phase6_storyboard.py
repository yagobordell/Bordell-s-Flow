import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.bots import StoryboardFrameBot
from ai_video_factory.config import settings
from ai_video_factory.domain import Shot, ShotTiming, VisualReference
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.storyboard_frames import build_storyboard_frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build provider-neutral storyboard keyframe prompts for timed shots."
    )
    parser.add_argument(
        "--shots",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
    )
    parser.add_argument(
        "--timings",
        type=Path,
        default=settings.output_dir / "phase5" / "shot_timings.json",
    )
    parser.add_argument(
        "--references",
        type=Path,
        default=settings.output_dir / "phase4" / "visual_references.json",
    )
    parser.add_argument(
        "--style",
        default="cinematic documentary",
        help="Shared storyboard visual style.",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="9:16",
        help="Storyboard frame aspect ratio.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_frames.json",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")

    shots = _read_models(args.shots, Shot)
    timings = _read_models(args.timings, ShotTiming)
    references = _read_models(args.references, VisualReference)

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    frame_bot = StoryboardFrameBot(provider=provider, model=settings.openai_model)
    frames = await build_storyboard_frames(
        shots,
        timings,
        references,
        frame_bot=frame_bot,
        visual_style=args.style,
        aspect_ratio=args.aspect_ratio,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([frame.model_dump() for frame in frames], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 6 storyboard planning complete. Artifact: {args.output.resolve()}")
    print(f"Built {len(frames)} ordered storyboard keyframe prompts")


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
