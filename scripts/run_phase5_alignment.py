import argparse
import asyncio
import json
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain import NarrationAudio, SourceScript
from ai_video_factory.providers import OpenAITranscriptionProvider
from ai_video_factory.workflows.narration_alignment import align_narration_words


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract word-level timestamps from the canonical narration audio."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=settings.output_dir / "phase2" / "source_script.json",
        help="Phase 2 source_script.json file.",
    )
    parser.add_argument(
        "--narration",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.json",
        help="Phase 5 narration.json metadata file.",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.wav",
        help="Phase 5 canonical narration WAV file.",
    )
    parser.add_argument(
        "--model",
        default=settings.openai_alignment_model,
        help="Transcription model used for word timestamps.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional ISO-639-1 language hint, for example es or en.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase5" / "narration_words.json",
        help="Path where NarrationWord entries will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    for path in (args.source, args.narration, args.audio):
        if not path.is_file():
            raise SystemExit(f"Required Phase 5 input not found: {path}")

    source = SourceScript.model_validate_json(args.source.read_text(encoding="utf-8"))
    narration = NarrationAudio.model_validate_json(args.narration.read_text(encoding="utf-8"))
    audio = args.audio.read_bytes()

    provider = OpenAITranscriptionProvider(api_key=settings.openai_api_key)
    words = await align_narration_words(
        source,
        narration,
        audio,
        transcription_provider=provider,
        model=args.model,
        language=args.language,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [word.model_dump() for word in words]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 5 word alignment complete. Artifact: {args.output.resolve()}")
    print(
        f"Aligned {len(words)} words from {words[0].start_seconds:.3f}s "
        f"to {words[-1].end_seconds:.3f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
