import argparse
import asyncio
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.providers import (
    BreezeFallbackEligibleError,
    FishSpeechReference,
    SaladBreezeSpeechProvider,
    SaladFishSpeechProvider,
)
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.providers.speech import GeneratedSpeech, SpeechProvider
from ai_video_factory.workers.breeze_tts2 import BREEZE_TTS2_GENERATION_PROFILE
from ai_video_factory.workflows.narration_audio import generate_narration_audio

DEFAULT_INSTRUCTIONS = (
    "Natural English documentary narration. Clear, engaging, measured delivery with restrained "
    "dramatic emphasis and consistent pacing across the entire script."
)
FALLBACK_ELIGIBLE_EXIT_CODE = 75


class CapturingSpeechProvider:
    """Capture provider-neutral provenance without changing the narration domain contract."""

    def __init__(
        self,
        provider: SpeechProvider,
        *,
        fallback_reason: str | None = None,
    ) -> None:
        self._provider = provider
        self._fallback_reason = fallback_reason
        self.metadata: dict[str, Any] = {}

    async def generate_speech(self, **kwargs: Any) -> GeneratedSpeech:
        generated = await self._provider.generate_speech(**kwargs)
        metadata = dict(generated.metadata)
        if self._fallback_reason:
            metadata["fallback_from"] = "breeze_tts2"
            metadata["fallback_reason"] = self._fallback_reason
        self.metadata = metadata
        return replace(generated, metadata=metadata)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one controlled Phase 5 speech-provider attempt. "
            "Use run_phase5_audio_controlled.ps1 for production Breeze->Fish routing."
        )
    )
    parser.add_argument(
        "source_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase2" / "source_script.json",
        help="Phase 2 source_script.json file.",
    )
    parser.add_argument(
        "--provider",
        choices=("breeze", "fish"),
        default="breeze",
        help="Provider for this attempt; the controlled wrapper serializes fallback GPU lifecycle.",
    )
    parser.add_argument("--primary-model", default=settings.breeze_tts_model)
    parser.add_argument(
        "--primary-voice",
        default=settings.breeze_tts_voice,
        help="Natural-language Breeze voice description.",
    )
    parser.add_argument("--fallback-model", default=settings.fish_speech_model)
    parser.add_argument(
        "--fish-reference-profile",
        default=settings.fish_speech_reference_profile,
        help=(
            "Authorized Fish reference identity/profile ID; "
            "never a natural-language voice prompt."
        ),
    )
    parser.add_argument(
        "--fish-reference-audio-key",
        default=settings.fish_speech_reference_audio_key,
        help="R2 object key for the authorized reference WAV.",
    )
    parser.add_argument(
        "--fish-reference-audio-sha256",
        default=settings.fish_speech_reference_audio_sha256,
        help="SHA-256 of the exact authorized reference WAV.",
    )
    parser.add_argument(
        "--fish-reference-transcript",
        default=settings.fish_speech_reference_transcript,
        help="Exact transcript corresponding to the authorized reference WAV.",
    )
    parser.add_argument(
        "--allow-unconditioned-fish",
        action="store_true",
        help="Technical smoke only. Production fallback requires an authorized reference.",
    )
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS)
    parser.add_argument("--cfg-scale", type=float, default=settings.breeze_tts_cfg_scale)
    parser.add_argument("--primary-seed", type=int, default=settings.breeze_tts_seed)
    parser.add_argument("--fallback-seed", type=int, default=settings.fish_speech_seed)
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=settings.inference_client_poll_seconds,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=settings.inference_client_timeout_seconds,
    )
    parser.add_argument(
        "--pending-timeout-seconds",
        type=float,
        default=settings.inference_client_timeout_seconds,
    )
    parser.add_argument(
        "--fallback-reason",
        default=None,
        help="Classification supplied only by the controlled wrapper for a Fish attempt.",
    )
    parser.add_argument(
        "--fallback-state",
        type=Path,
        default=settings.output_dir / "phase5" / "fallback-state.json",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate provider configuration without submitting an inference job.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase5",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.json",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        default=settings.output_dir / "phase5" / "narration.provenance.json",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _build_reference(args: argparse.Namespace) -> FishSpeechReference | None:
    values = {
        "FISH_SPEECH_REFERENCE_PROFILE": args.fish_reference_profile,
        "FISH_SPEECH_REFERENCE_AUDIO_KEY": args.fish_reference_audio_key,
        "FISH_SPEECH_REFERENCE_AUDIO_SHA256": args.fish_reference_audio_sha256,
        "FISH_SPEECH_REFERENCE_TRANSCRIPT": args.fish_reference_transcript,
    }
    present = {name: value for name, value in values.items() if value is not None and value.strip()}
    if not present:
        return None
    if len(present) != len(values):
        missing = sorted(set(values) - set(present))
        raise SystemExit(
            "Fish reference configuration is incomplete; missing: " + ", ".join(missing)
        )
    return FishSpeechReference(
        profile=present["FISH_SPEECH_REFERENCE_PROFILE"].strip(),
        audio_key=present["FISH_SPEECH_REFERENCE_AUDIO_KEY"].strip(),
        audio_sha256=present["FISH_SPEECH_REFERENCE_AUDIO_SHA256"].strip().lower(),
        transcript=present["FISH_SPEECH_REFERENCE_TRANSCRIPT"],
    )


def _validate_reference_object(storage: Any, reference: FishSpeechReference | None) -> None:
    if reference is None:
        return

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


def _executor(
    *,
    storage: Any,
    args: argparse.Namespace,
) -> InferenceJobExecutor:
    queue = PostgresJobQueueClient(
        dsn=_required_setting("POSTGRES_DSN", settings.postgres_dsn),
    )
    return InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )


async def main() -> None:
    args = parse_args()
    if not args.source_file.is_file():
        raise SystemExit(f"Source script file not found: {args.source_file}")

    source = SourceScript.model_validate_json(args.source_file.read_text(encoding="utf-8"))
    storage = create_r2_storage(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )

    if args.provider == "breeze":
        executor = _executor(storage=storage, args=args)
        provider: SpeechProvider = SaladBreezeSpeechProvider(
            executor=executor,
            temp_dir=settings.temp_dir / "breeze-client",
            cfg_scale=args.cfg_scale,
            seed=args.primary_seed,
        )
        model = args.primary_model
        voice = args.primary_voice
    else:
        reference = _build_reference(args)
        _validate_reference_object(storage, reference)
        executor = _executor(storage=storage, args=args)
        provider = SaladFishSpeechProvider(
            executor=executor,
            temp_dir=settings.temp_dir / "fish-speech-client",
            reference=reference,
            seed=args.fallback_seed,
            allow_unconditioned=args.allow_unconditioned_fish,
        )
        model = args.fallback_model
        voice = reference.profile if reference is not None else "unconditioned"

    if args.preflight_only:
        print(f"PHASE5_PROVIDER_PREFLIGHT provider={args.provider} ok=true")
        return

    capturing = CapturingSpeechProvider(
        provider,
        fallback_reason=args.fallback_reason if args.provider == "fish" else None,
    )
    try:
        narration = await generate_narration_audio(
            source,
            speech_provider=capturing,
            output_dir=args.output_dir,
            model=model,
            voice=voice,
            instructions=args.instructions,
            speed=args.speed,
        )
    except BreezeFallbackEligibleError as exc:
        if args.provider != "breeze":
            raise
        args.fallback_state.parent.mkdir(parents=True, exist_ok=True)
        args.fallback_state.write_text(
            json.dumps(
                {
                    "fallback_from": "breeze_tts2",
                    "fallback_reason": exc.reason,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"PHASE5_FALLBACK_ELIGIBLE reason={exc.reason}")
        raise SystemExit(FALLBACK_ELIGIBLE_EXIT_CODE) from exc

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(narration.model_dump_json(indent=2), encoding="utf-8")
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        **capturing.metadata,
        "canonical_uri": narration.uri,
        "canonical_duration_seconds": narration.duration_seconds,
    }
    args.provenance.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    args.fallback_state.unlink(missing_ok=True)

    print(f"Phase 5 narration audio complete. Metadata: {args.metadata.resolve()}")
    print(f"Provider provenance: {args.provenance.resolve()}")
    print(f"Measured narration duration: {narration.duration_seconds:.3f} seconds")
    print(f"Breeze generation profile: {BREEZE_TTS2_GENERATION_PROFILE}")
    print(f"Generated with provider={args.provider} model={model}")


if __name__ == "__main__":
    asyncio.run(main())
