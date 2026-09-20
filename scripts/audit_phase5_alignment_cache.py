from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_whisper import build_whisper_job_request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit deterministic Whisper word alignment in R2 without touching Salad."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--model", default=settings.whisper_model)
    parser.add_argument("--language", default="en")
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def main() -> None:
    args = parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Source script not found: {args.source}")
    if not args.audio.is_file():
        raise SystemExit(f"Narration audio not found: {args.audio}")

    SourceScript.model_validate_json(args.source.read_text(encoding="utf-8"))
    audio_sha256 = hashlib.sha256(args.audio.read_bytes()).hexdigest()
    request = build_whisper_job_request(
        audio_sha256=audio_sha256,
        filename=args.audio.name,
        model=args.model,
        language=args.language,
    )
    storage = create_r2_storage(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )

    error: str | None = None
    try:
        cached = cached_inference_response(storage, request)
    except RuntimeError as exc:
        cached = None
        status = "invalid"
        error = str(exc)
    else:
        status = "hit" if cached is not None else "miss"

    record = {
        "status": status,
        "provider": "whisper",
        "application_job_id": request.job_id,
        "request_sha256": request.fingerprint(),
        "object_key": request.output.key,
        "audio_sha256": audio_sha256,
        "language": args.language,
        "generation_profile": request.parameters["generation_profile"],
        "error": error,
    }
    print(
        "Phase 5 Whisper cache: "
        f"status={status} job={request.job_id} Salad submissions=0"
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Audit JSON: {args.json_output.resolve()}")
    if status == "invalid":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
