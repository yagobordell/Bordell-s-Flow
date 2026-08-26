import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ai_video_factory.bots import VisualReferenceBot
from ai_video_factory.config import settings
from ai_video_factory.domain import ContinuityEntity
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.visual_references import build_visual_references


DEFAULT_VISUAL_STYLE = "cinematic documentary"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical visual reference prompts from Phase 3 continuity entities."
    )
    parser.add_argument(
        "entities_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase3" / "entities.json",
        help="Phase 3 entities.json file.",
    )
    parser.add_argument(
        "--style",
        default=DEFAULT_VISUAL_STYLE,
        help="Shared visual style applied to all canonical references.",
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
    if not args.entities_file.is_file():
        raise SystemExit(f"Continuity entities file not found: {args.entities_file}")

    raw_entities: Any = json.loads(args.entities_file.read_text(encoding="utf-8"))
    if not isinstance(raw_entities, list):
        raise SystemExit("Continuity entities file must contain a JSON array.")

    entities = [ContinuityEntity.model_validate(item) for item in raw_entities]
    provider = OpenAIProvider(api_key=settings.openai_api_key)
    reference_bot = VisualReferenceBot(provider=provider, model=settings.openai_model)
    references = await build_visual_references(
        entities,
        reference_bot=reference_bot,
        visual_style=args.style,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [reference.model_dump() for reference in references]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 4 visual references complete. Artifact written to: {args.output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
