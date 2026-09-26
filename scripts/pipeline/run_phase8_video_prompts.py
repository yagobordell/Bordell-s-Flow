import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.legacy_bots import VideoPromptBot
from ai_video_factory.config import settings
from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.video_prompts import build_video_prompts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build provider-neutral motion prompts for storyboard-conditioned video shots."
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
        "--storyboard-frames",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_frames.json",
    )
    parser.add_argument(
        "--style",
        default="cinematic documentary",
        help="Shared video visual style.",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=("16:9",),
        help="Target cinematic landscape video aspect ratio.",
    )
    parser.add_argument(
        "--max-scene-concurrency",
        type=int,
        default=3,
        help="Maximum number of scenes planned concurrently; shots stay serial within each scene.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase8" / "video_prompts.json",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")

    shots = _read_models(args.shots, Shot)
    timings = _read_models(args.timings, ShotTiming)
    storyboard_frames = _read_models(args.storyboard_frames, StoryboardFrame)

    provider = OpenAIProvider(
        api_key=settings.openai_api_key,
        reasoning_effort="low",
    )
    prompt_bot = VideoPromptBot(provider=provider, model=settings.openai_model)
    prompts = await build_video_prompts(
        shots,
        timings,
        storyboard_frames,
        prompt_bot=prompt_bot,
        visual_style=args.style,
        aspect_ratio=args.aspect_ratio,
        max_scene_concurrency=args.max_scene_concurrency,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([prompt.model_dump() for prompt in prompts], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 8.1 video motion planning complete. Artifact: {args.output.resolve()}")
    print(f"Built {len(prompts)} ordered video motion prompts")


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
