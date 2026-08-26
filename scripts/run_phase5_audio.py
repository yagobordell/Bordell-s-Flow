import argparse
import asyncio
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.providers import OpenAISpeechProvider
from ai_video_factory.workflows.narration_audio import generate_narration_audio

DEFAULT_INSTRUCTIONS = (
    "Natural documentary narration in the language of the provided script. "
    "Clear, engaging, measured delivery with restrained dramatic emphasis."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one canonical narration WAV and measure its real duration."
    )
    parser.add_argument(
        "source_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase2" / "source_script.json",
        help="Phase 2 source_script.json file.",
    )
    parser.add_argument(
        "--model",
        default=settings.openai_tts_model,
        help="Text-to-speech model.",
    )
    parser.add_argument(
        "--voice",
        default=settings.openai_tts_voice,
        help="Text-to-speech voice.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Speech speed from 0.25 to 4.0.",
    )
    parser.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="Voice style instructions passed to the TTS provider.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase5",
        help="Directory where narration.wav will be written.",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.json",
        help="Path where NarrationAudio metadata will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Add it to your local .env file.")
    if not args.source_file.is_file():
        raise SystemExit(f"Source script file not found: {args.source_file}")

    source = SourceScript.model_validate_json(args.source_file.read_text(encoding="utf-8"))
    provider = OpenAISpeechProvider(api_key=settings.openai_api_key)
    narration = await generate_narration_audio(
        source,
        speech_provider=provider,
        output_dir=args.output_dir,
        model=args.model,
        voice=args.voice,
        instructions=args.instructions,
        speed=args.speed,
    )

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(narration.model_dump_json(indent=2), encoding="utf-8")

    print(f"Phase 5 narration audio complete. Metadata: {args.metadata.resolve()}")
    print(f"Measured narration duration: {narration.duration_seconds:.3f} seconds")


if __name__ == "__main__":
    asyncio.run(main())
