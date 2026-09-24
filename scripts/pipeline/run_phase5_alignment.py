import argparse
import asyncio
import json
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain import NarrationAudio, SourceScript
from ai_video_factory.providers import SaladWhisperTranscriptionProvider
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workflows.narration_alignment import align_narration_words


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract word-level timestamps with the dedicated Salad Whisper worker."
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
        default=settings.whisper_model,
        help="Whisper model hosted by the dedicated Salad worker.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Whisper language hint; defaults to en for the English video pipeline.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=settings.inference_client_poll_seconds,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=settings.inference_client_timeout_seconds,
        help="Maximum seconds after a worker claims the Postgres job.",
    )
    parser.add_argument(
        "--pending-timeout-seconds",
        type=float,
        default=settings.inference_client_timeout_seconds,
        help="Maximum wait before a worker claims the Postgres job.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase5" / "narration_words.json",
        help="Path where NarrationWord entries will be written.",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


async def main() -> None:
    args = parse_args()

    for path in (args.source, args.narration, args.audio):
        if not path.is_file():
            raise SystemExit(f"Required Phase 5 input not found: {path}")

    source = SourceScript.model_validate_json(args.source.read_text(encoding="utf-8"))
    narration = NarrationAudio.model_validate_json(args.narration.read_text(encoding="utf-8"))
    audio = args.audio.read_bytes()

    storage = create_r2_storage(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    queue = PostgresJobQueueClient(
        dsn=_required_setting("POSTGRES_DSN", settings.postgres_dsn),
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    provider = SaladWhisperTranscriptionProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "whisper-client",
    )
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
        f"to {words[-1].end_seconds:.3f}s with {args.model} via the Postgres job transport"
    )


if __name__ == "__main__":
    asyncio.run(main())
