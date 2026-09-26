import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.config import settings
from ai_video_factory.domain import BlockContinuity, ContinuityEntity, NarrativeBlock
from ai_video_factory.legacy_bots import VisualReferenceBot
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.visual_references import build_visual_references

DEFAULT_VISUAL_STYLE = "cinematic documentary"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical visual reference prompts from continuity entities and context."
    )
    parser.add_argument(
        "entities_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase3" / "entities.json",
        help="Phase 3 entities.json file.",
    )
    parser.add_argument(
        "--blocks",
        type=Path,
        default=settings.output_dir / "phase2" / "narrative_blocks.json",
        help="Phase 2 narrative_blocks.json file used as visual context.",
    )
    parser.add_argument(
        "--continuity",
        type=Path,
        default=settings.output_dir / "phase3" / "block_continuity.json",
        help="Phase 3 block_continuity.json file used to bind entities to narrative context.",
    )
    parser.add_argument(
        "--style",
        default=DEFAULT_VISUAL_STYLE,
        help="Shared visual style applied to all canonical references.",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="16:9",
        choices=("16:9",),
        help="Canonical production reference aspect ratio.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase4" / "visual_references.json",
        help="Path where Phase 4 visual reference prompts will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")

    entities = _read_models(args.entities_file, ContinuityEntity)
    blocks = _read_models(args.blocks, NarrativeBlock)
    continuity = _read_models(args.continuity, BlockContinuity)

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    reference_bot = VisualReferenceBot(provider=provider, model=settings.openai_model)
    references = await build_visual_references(
        entities,
        narrative_blocks=blocks,
        block_continuity=continuity,
        reference_bot=reference_bot,
        visual_style=args.style,
        aspect_ratio=args.aspect_ratio,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [reference.model_dump() for reference in references]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 4 visual references complete. Artifact written to: {args.output.resolve()}")


def _read_models[T: BaseModel](path: Path, model_type: type[T]) -> list[T]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")

    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {path}")

    return [model_type.model_validate(item) for item in raw]


if __name__ == "__main__":
    asyncio.run(main())
