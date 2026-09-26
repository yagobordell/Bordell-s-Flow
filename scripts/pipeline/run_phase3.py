import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ai_video_factory.legacy_bots import ContinuityBot
from ai_video_factory.config import settings
from ai_video_factory.domain import NarrativeBlock
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.continuity import plan_continuity


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a stateful continuity registry from Phase 2 narrative blocks."
    )
    parser.add_argument(
        "blocks_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase2" / "narrative_blocks.json",
        help="Phase 2 narrative_blocks.json file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase3",
        help="Directory where Phase 3 continuity artifacts will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    if not args.blocks_file.is_file():
        raise SystemExit(f"Narrative blocks file not found: {args.blocks_file}")

    raw_blocks: Any = json.loads(args.blocks_file.read_text(encoding="utf-8"))
    if not isinstance(raw_blocks, list):
        raise SystemExit("Narrative blocks file must contain a JSON array.")

    blocks = [NarrativeBlock.model_validate(item) for item in raw_blocks]

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    continuity_bot = ContinuityBot(provider=provider, model=settings.openai_model)
    entities, block_continuity = await plan_continuity(
        blocks,
        continuity_bot=continuity_bot,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    _write_models(args.output / "entities.json", entities)
    _write_models(args.output / "block_continuity.json", block_continuity)

    print(f"Phase 3 continuity complete. Artifacts written to: {args.output.resolve()}")


def _write_models(path: Path, models: list[object]) -> None:
    payload = [model.model_dump() for model in models]  # type: ignore[attr-defined]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
