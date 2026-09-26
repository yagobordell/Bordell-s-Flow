import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel

from ai_video_factory.legacy_bots import BeatTimingBot
from ai_video_factory.config import settings
from ai_video_factory.domain import Beat, NarrationAudio, NarrationWord, SourceScript
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.beat_timing import build_beat_timings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align narrative beats to word-level narration timestamps."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=settings.output_dir / "phase2" / "source_script.json",
    )
    parser.add_argument(
        "--beats",
        type=Path,
        default=settings.output_dir / "phase2" / "beats.json",
    )
    parser.add_argument(
        "--narration",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.json",
    )
    parser.add_argument(
        "--words",
        type=Path,
        default=settings.output_dir / "phase5" / "narration_words.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase5" / "beat_timings.json",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    for path in (args.source, args.beats, args.narration, args.words):
        if not path.is_file():
            raise SystemExit(f"Required Phase 5 input not found: {path}")

    source = SourceScript.model_validate_json(args.source.read_text(encoding="utf-8"))
    beats = _read_models(args.beats, Beat)
    narration = NarrationAudio.model_validate_json(args.narration.read_text(encoding="utf-8"))
    words = _read_models(args.words, NarrationWord)

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    timing_bot = BeatTimingBot(provider=provider, model=settings.openai_model)
    timings = await build_beat_timings(
        source,
        beats,
        words,
        narration,
        timing_bot=timing_bot,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [timing.model_dump() for timing in timings]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 5 beat timing complete. Artifact: {args.output.resolve()}")
    print(
        f"Aligned {len(timings)} beats across "
        f"{timings[-1].end_seconds:.3f} seconds of narration"
    )


def _read_models[T: BaseModel](path: Path, model_type: type[T]) -> list[T]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return [model_type.model_validate(item) for item in payload]


if __name__ == "__main__":
    asyncio.run(main())
