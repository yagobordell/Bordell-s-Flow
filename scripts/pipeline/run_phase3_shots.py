import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.bots import ShotPlannerBot
from ai_video_factory.config import settings
from ai_video_factory.domain import Beat, BlockContinuity, ContinuityEntity, Scene
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.shot_planning import plan_shots


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan stateful shots from Phase 2 scenes and Phase 3 continuity artifacts."
    )
    parser.add_argument(
        "--beats",
        type=Path,
        default=settings.output_dir / "phase2" / "beats.json",
        help="Phase 2 beats.json file.",
    )
    parser.add_argument(
        "--scenes",
        type=Path,
        default=settings.output_dir / "phase2" / "scenes.json",
        help="Phase 2 scenes.json file.",
    )
    parser.add_argument(
        "--entities",
        type=Path,
        default=settings.output_dir / "phase3" / "entities.json",
        help="Phase 3 entities.json file.",
    )
    parser.add_argument(
        "--continuity",
        type=Path,
        default=settings.output_dir / "phase3" / "block_continuity.json",
        help="Phase 3 block_continuity.json file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
        help="Path where planned shots will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")

    beats = _read_models(args.beats, Beat)
    scenes = _read_models(args.scenes, Scene)
    entities = _read_models(args.entities, ContinuityEntity)
    continuity = _read_models(args.continuity, BlockContinuity)

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    shot_bot = ShotPlannerBot(provider=provider, model=settings.openai_model)
    shots = await plan_shots(
        scenes,
        beats=beats,
        entities=entities,
        block_continuity=continuity,
        shot_bot=shot_bot,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [shot.model_dump() for shot in shots]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 3 shot planning complete. Artifact written to: {args.output.resolve()}")


def _read_models[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> list[ModelT]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")

    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {path}")

    return [model_type.model_validate(item) for item in raw]


if __name__ == "__main__":
    asyncio.run(main())
