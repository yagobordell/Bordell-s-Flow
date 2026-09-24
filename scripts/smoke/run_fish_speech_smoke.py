import argparse
import asyncio
import hashlib
import json
import tempfile
import time
import wave
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_fish_speech import (
    FishSpeechReference,
    SaladFishSpeechProvider,
)
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.workers.fish_speech import FISH_SPEECH_MODEL_ID
from r2_client import create_r2_storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real Salad Fish Speech S2 Pro smoke.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "fish-speech-smoke",
    )
    parser.add_argument("--expect-replay", action="store_true")
    parser.add_argument("--allow-unconditioned", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is required for the real Fish Salad smoke.")
    return value.strip()


def build_reference() -> FishSpeechReference | None:
    values = (
        settings.fish_speech_reference_profile,
        settings.fish_speech_reference_audio_key,
        settings.fish_speech_reference_audio_sha256,
        settings.fish_speech_reference_transcript,
    )
    if not any(values):
        return None
    if not all(values):
        raise SystemExit("Fish reference configuration is incomplete.")
    return FishSpeechReference(
        profile=str(values[0]),
        audio_key=str(values[1]),
        audio_sha256=str(values[2]).lower(),
        transcript=str(values[3]),
    )


def build_storage():
    return create_r2_storage(
        endpoint_url=required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=required("R2_BUCKET", settings.r2_bucket),
        access_key_id=required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )


def validate_reference_object(reference: FishSpeechReference | None) -> None:
    if reference is None:
        return

    storage = build_storage()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.temp_dir) as directory:
        destination = Path(directory) / "fish-reference.wav"
        try:
            stored = storage.download(reference.audio_key, destination)
        except FileNotFoundError as exc:
            raise SystemExit(
                f"Fish reference R2 object does not exist: {reference.audio_key}"
            ) from exc

        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest != reference.audio_sha256.lower():
            raise SystemExit(
                "Fish reference R2 SHA-256 mismatch: "
                f"expected={reference.audio_sha256.lower()} actual={digest}"
            )
        if stored.content_type not in {"audio/wav", "audio/x-wav"}:
            raise SystemExit(
                "Fish reference R2 object must have WAV content type; "
                f"got {stored.content_type!r}"
            )


def build_provider(*, allow_unconditioned: bool) -> SaladFishSpeechProvider:
    storage = build_storage()
    queue = PostgresJobQueueClient(
        dsn=required("POSTGRES_DSN", settings.postgres_dsn),
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=settings.inference_client_poll_seconds,
        timeout_seconds=settings.inference_client_timeout_seconds,
        pending_timeout_seconds=settings.inference_client_timeout_seconds,
    )
    return SaladFishSpeechProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "fish-speech-smoke",
        reference=build_reference(),
        seed=settings.fish_speech_seed,
        allow_unconditioned=allow_unconditioned,
    )


def inspect_wav(path: Path) -> dict[str, int | float]:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_count = wav_file.getnframes()
    if sample_rate != 24_000 or channels != 1 or sample_width != 2 or frame_count <= 0:
        raise RuntimeError("Fish real smoke did not produce canonical PCM16 mono 24 kHz WAV")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
        "duration_seconds": frame_count / sample_rate,
    }


async def main() -> None:
    args = parse_args()
    reference = build_reference()
    if reference is None and not args.allow_unconditioned:
        raise SystemExit(
            "No authorized Fish reference is configured. Set FISH_SPEECH_REFERENCE_* "
            "or use --allow-unconditioned only for a technical runtime smoke."
        )

    validate_reference_object(reference)
    provider = build_provider(allow_unconditioned=args.allow_unconditioned)
    if args.preflight_only:
        print(
            "FISH_SPEECH_SMOKE_PREFLIGHT "
            f"reference_conditioned={str(reference is not None).lower()} ok=true"
        )
        return

    voice = reference.profile if reference is not None else "unconditioned"
    text = (
        "Fish Speech Salad smoke session "
        f"{args.session_id}. This sentence validates real self hosted narration."
    )
    started = time.monotonic()
    speech = await provider.generate_speech(
        text=text,
        model=FISH_SPEECH_MODEL_ID,
        voice=voice,
        instructions=(
            "Natural English documentary narration. Clear, engaging, measured delivery."
        ),
        speed=1.0,
        output_format="wav",
    )
    elapsed = time.monotonic() - started

    replayed = bool(speech.metadata["replayed"])
    if args.expect_replay and not replayed:
        raise RuntimeError("Fish replay smoke expected replayed=true but inference ran again")
    if not args.expect_replay and replayed:
        raise RuntimeError(
            "Fish first-pass smoke unexpectedly replayed; use a fresh --session-id "
            "to prove real inference."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = args.output_dir / "audio.wav"
    audio_path.write_bytes(speech.content)
    wav = inspect_wav(audio_path)
    output_sha = hashlib.sha256(speech.content).hexdigest()
    if output_sha != speech.metadata["output_sha256"]:
        raise RuntimeError("Downloaded Fish artifact SHA-256 does not match provider metadata")

    report = {
        "session_id": args.session_id,
        "elapsed_total_seconds": elapsed,
        "output_bytes": len(speech.content),
        "output_sha256": output_sha,
        "reference_conditioned": reference is not None,
        "expect_replay": args.expect_replay,
        **wav,
        **dict(speech.metadata),
    }
    report_path = args.output_dir / ("replay-report.json" if args.expect_replay else "report.json")
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "FISH_SPEECH_SMOKE "
        f"replayed={str(replayed).lower()} elapsed_seconds={elapsed:.3f} "
        f"audio_seconds={wav['duration_seconds']:.3f} sha256={output_sha}"
    )
    print(report_path.resolve())


if __name__ == "__main__":
    asyncio.run(main())
