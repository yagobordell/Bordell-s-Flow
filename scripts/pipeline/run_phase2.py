import argparse
import asyncio
import json
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.legacy_bots import BeatExtractorBot, NarrativeBlockBot, ScenePlannerBot
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.narrative_planning import plan_narrative


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan a finished script as narrative blocks, beats, and scenes."
    )
    parser.add_argument(
        "script_file",
        type=Path,
        help="UTF-8 text file containing the final script",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase2",
        help="Directory where Phase 2 JSON artifacts will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    if not args.script_file.is_file():
        raise SystemExit(f"Script file not found: {args.script_file}")

    script_text = args.script_file.read_text(encoding="utf-8").strip()
    source = SourceScript(text=script_text)

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    block_bot = NarrativeBlockBot(provider=provider, model=settings.openai_model)
    beat_bot = BeatExtractorBot(provider=provider, model=settings.openai_model)
    scene_bot = ScenePlannerBot(provider=provider, model=settings.openai_model)

    blocks, beats, scenes = await plan_narrative(
        source,
        block_bot=block_bot,
        beat_bot=beat_bot,
        scene_bot=scene_bot,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "source_script.json").write_text(
        source.model_dump_json(indent=2), encoding="utf-8"
    )
    _write_models(args.output / "narrative_blocks.json", blocks)
    _write_models(args.output / "beats.json", beats)
    _write_models(args.output / "scenes.json", scenes)

    print(f"Phase 2 complete. Artifacts written to: {args.output.resolve()}")


def _write_models(path: Path, models: list[object]) -> None:
    payload = [model.model_dump() for model in models]  # type: ignore[attr-defined]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
