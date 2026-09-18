import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.providers.fallback_speech import BreezeThenFishSpeechProvider
from ai_video_factory.providers.salad_breeze import BreezeFallbackEligibleError
from ai_video_factory.providers.speech import SpeechProvider
from ai_video_factory.workers.fish_speech import FISH_SPEECH_MODEL_ID

from run_fish_speech_smoke import build_provider, build_reference


class EligibleFakeBreeze:
    async def generate_speech(self, **kwargs):
        raise BreezeFallbackEligibleError(
            "breeze_test_eligible_failure",
            "Synthetic eligible Breeze failure; no Breeze job was submitted.",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route fake eligible Breeze failure into real Salad Fish Speech."
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "fish-speech-smoke",
    )
    parser.add_argument("--allow-unconditioned", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    reference = build_reference()
    if reference is None and not args.allow_unconditioned:
        raise SystemExit(
            "Routing smoke requires an authorized Fish reference unless "
            "--allow-unconditioned is explicitly used for technical validation."
        )
    fish = build_provider(allow_unconditioned=args.allow_unconditioned)
    provider = BreezeThenFishSpeechProvider(
        primary=EligibleFakeBreeze(),  # type: ignore[arg-type]
        fallback=fish,
        fallback_model=FISH_SPEECH_MODEL_ID,
        fallback_voice=reference.profile if reference is not None else "unconditioned",
    )
    started = time.monotonic()
    speech = await provider.generate_speech(
        text=(
            "Fallback routing smoke "
            f"{args.session_id}. Breeze is fake and Fish is the real Salad worker."
        ),
        model=settings.breeze_tts_model,
        voice=settings.breeze_tts_voice,
        instructions="Natural English documentary narration with measured delivery.",
        speed=1.0,
        output_format="wav",
    )
    elapsed = time.monotonic() - started

    metadata = dict(speech.metadata)
    if metadata.get("provider") != "fish_speech":
        raise RuntimeError("Routing smoke did not return Fish provider metadata")
    if metadata.get("fallback_from") != "breeze_tts2":
        raise RuntimeError("Routing smoke did not record fallback_from=breeze_tts2")
    if metadata.get("fallback_reason") != "breeze_test_eligible_failure":
        raise RuntimeError("Routing smoke did not preserve the exact eligible failure reason")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = args.output_dir / "fallback-routing.wav"
    audio_path.write_bytes(speech.content)
    report = {
        "session_id": args.session_id,
        "elapsed_total_seconds": elapsed,
        "output_bytes": len(speech.content),
        "output_sha256": hashlib.sha256(speech.content).hexdigest(),
        "breeze_gpu_used": False,
        **metadata,
    }
    report_path = args.output_dir / "fallback-routing-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(
        "FISH_SPEECH_FALLBACK_ROUTING_SMOKE "
        f"fallback_from={metadata['fallback_from']} "
        f"reason={metadata['fallback_reason']} elapsed_seconds={elapsed:.3f}"
    )
    print(report_path.resolve())


if __name__ == "__main__":
    asyncio.run(main())
