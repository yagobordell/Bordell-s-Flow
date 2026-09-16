import argparse
import asyncio
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.inference.storage import R2ObjectStorage
from ai_video_factory.providers import SaladBreezeSpeechProvider
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workflows.narration_audio import generate_narration_audio

DEFAULT_INSTRUCTIONS = (
    "Natural English documentary narration. Clear, engaging, measured delivery with restrained "
    "dramatic emphasis and consistent pacing across the entire script."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one canonical narration WAV with the Salad Breeze TTS 2 worker."
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
        default=settings.breeze_tts_model,
        help="Breeze TTS 2 model hosted by the dedicated Salad worker.",
    )
    parser.add_argument(
        "--voice",
        default=settings.breeze_tts_voice,
        help="Natural-language voice identity description passed to Breeze TTS 2.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Final narration speed from 0.25 to 4.0.",
    )
    parser.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="Delivery direction passed to Breeze TTS 2.",
    )
    parser.add_argument(
        "--cfg-scale",
        type=float,
        default=settings.breeze_tts_cfg_scale,
        help="Breeze instruction guidance scale.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=settings.breeze_tts_seed,
        help="Deterministic Breeze generation seed.",
    )
    parser.add_argument(
        "--queue-name",
        default=settings.salad_breeze_tts2_queue_name,
        help="Dedicated Salad queue used by the Breeze TTS 2 worker.",
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
        help="Maximum seconds after Salad dispatches the job to a ready worker.",
    )
    parser.add_argument(
        "--pending-timeout-seconds",
        type=float,
        default=settings.inference_client_timeout_seconds,
        help="Maximum queue wait before dispatch; controlled prewarm uses a short value.",
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


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


async def main() -> None:
    args = parse_args()

    if not args.source_file.is_file():
        raise SystemExit(f"Source script file not found: {args.source_file}")

    source = SourceScript.model_validate_json(args.source_file.read_text(encoding="utf-8"))
    storage = R2ObjectStorage.create(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    queue = SaladJobQueueClient(
        organization=_required_setting("SALAD_ORGANIZATION", settings.salad_organization),
        project=_required_setting("SALAD_PROJECT", settings.salad_project),
        queue_name=args.queue_name,
        api_key=_required_setting("SALAD_API_KEY", settings.salad_api_key),
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    provider = SaladBreezeSpeechProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "breeze-client",
        cfg_scale=args.cfg_scale,
        seed=args.seed,
    )
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
    print(f"Generated with {args.model} via Salad queue {args.queue_name}")


if __name__ == "__main__":
    asyncio.run(main())
