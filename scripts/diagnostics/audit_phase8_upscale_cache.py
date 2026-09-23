from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from r2_client import create_r2_storage

from ai_video_factory.domain import VideoClip
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.workflows.video_upscale import build_video_upscale_plan

REQUIRED_ENV = (
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit deterministic Real-ESRGAN outputs in R2 without touching Salad."
    )
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def main() -> None:
    args = parse_args()
    raw = json.loads(args.clips.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {args.clips}")
    plan = build_video_upscale_plan(
        [VideoClip.model_validate(item) for item in raw],
        clip_base_dir=args.clips.parent,
    )
    environment = _environment()
    storage = create_r2_storage(
        endpoint_url=environment["R2_ENDPOINT_URL"],
        bucket=environment["R2_BUCKET"],
        access_key_id=environment["R2_ACCESS_KEY_ID"],
        secret_access_key=environment["R2_SECRET_ACCESS_KEY"],
    )
    records: list[dict[str, object]] = []
    for item in plan:
        status = "miss"
        error: str | None = None
        try:
            cached = cached_inference_response(storage, item.request)
        except RuntimeError as exc:
            cached = None
            status = "invalid"
            error = str(exc)
        if cached is not None:
            status = "hit"
        records.append(
            {
                "shot_id": item.shot_id,
                "status": status,
                "application_job_id": item.request.job_id,
                "request_sha256": item.request.fingerprint(),
                "source_sha256": item.source_sha256,
                "object_key": item.request.output.key,
                "error": error,
            }
        )

    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    print(
        f"Real-ESRGAN cache: hits={hits} misses={misses} invalid={invalid} "
        f"total={len(records)} Salad submissions=0"
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if invalid:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
