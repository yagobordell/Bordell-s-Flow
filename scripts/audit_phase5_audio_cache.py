from __future__ import annotations

import argparse
import json
from pathlib import Path

from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.domain import SourceScript
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_breeze import build_breeze_job_request

DEFAULT_INSTRUCTIONS = (
    "Natural English documentary narration. Clear, engaging, measured delivery with restrained "
    "dramatic emphasis and consistent pacing across the entire script."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the canonical Breeze narration output in R2 without touching Salad."
    )
    parser.add_argument("source_file", type=Path)
    parser.add_argument("--model", default=settings.breeze_tts_model)
    parser.add_argument("--voice", default=settings.breeze_tts_voice)
    parser.add_argument("--instructions", default=DEFAULT_INSTRUCTIONS)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--cfg-scale", type=float, default=settings.breeze_tts_cfg_scale)
    parser.add_argument("--seed", type=int, default=settings.breeze_tts_seed)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def main() -> None:
    args = parse_args()
    if not args.source_file.is_file():
        raise SystemExit(f"Source script file not found: {args.source_file}")
    source = SourceScript.model_validate_json(args.source_file.read_text(encoding="utf-8"))
    request = build_breeze_job_request(
        text=source.text,
        voice=args.voice,
        instructions=args.instructions,
        speed=args.speed,
        cfg_scale=args.cfg_scale,
        seed=args.seed,
        model_id=args.model,
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
        "provider": "breeze_tts2",
        "application_job_id": request.job_id,
        "request_sha256": request.fingerprint(),
        "object_key": request.output.key,
        "error": error,
    }
    print(
        "Phase 5 Breeze cache: "
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
