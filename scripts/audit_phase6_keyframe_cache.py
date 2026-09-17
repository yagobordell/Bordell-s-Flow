from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import StoryboardFrame
from ai_video_factory.inference.storage import R2ObjectStorage
from ai_video_factory.providers.ideogram_rejections import find_safety_rejection
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_flux import build_flux_job_request
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    parse_ideogram_size,
    reference_caption_variants,
)
from ai_video_factory.workers.flux_schnell import FLUX_SCHNELL_KEYFRAME_TASK
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_KEYFRAME_TASK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Phase 6 keyframe caches and Ideogram safety blocks without queue work."
    )
    parser.add_argument(
        "--frames",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_frames.json",
    )
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--flux-model", default=settings.flux_schnell_model)
    parser.add_argument("--size", default="1024x1536")
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def audit_keyframe_cache(
    frames: list[StoryboardFrame],
    *,
    storage: R2ObjectStorage,
    model_id: str,
    flux_model_id: str,
    size: str,
) -> list[dict[str, Any]]:
    width, height = parse_ideogram_size(size)
    records: list[dict[str, Any]] = []
    for frame in frames:
        candidates = [
            (
                name,
                build_ideogram_job_request(
                    task_name=IDEOGRAM4_KEYFRAME_TASK,
                    caption=caption,
                    model_id=model_id,
                    width=width,
                    height=height,
                ),
            )
            for name, caption in reference_caption_variants(
                frame.prompt,
                task_name=IDEOGRAM4_KEYFRAME_TASK,
            )
        ]
        flux_request = build_flux_job_request(
            task_name=FLUX_SCHNELL_KEYFRAME_TASK,
            caption=frame.prompt,
            model_id=flux_model_id,
            width=width,
            height=height,
        )
        record: dict[str, Any] = {
            "shot_id": frame.shot_id,
            "status": "miss",
            "primary_status": "miss",
            "matched_provider": None,
            "candidate_job_ids": {name: request.job_id for name, request in candidates},
            "safety_rejections": {},
            "fallback_job_id": flux_request.job_id,
            "fallback_status": "miss",
            "error": None,
        }
        for _name, request in candidates:
            try:
                cached = cached_inference_response(storage, request)
            except RuntimeError as exc:
                record["status"] = "invalid"
                record["primary_status"] = "invalid"
                record["error"] = str(exc)
                break
            if cached is not None:
                record["status"] = "hit"
                record["primary_status"] = "hit"
                record["matched_provider"] = "ideogram4"
                break
        if record["status"] in {"hit", "invalid"}:
            records.append(record)
            continue

        for name, request in candidates:
            rejection = find_safety_rejection(storage, request)
            if rejection is not None:
                record["safety_rejections"][name] = {
                    "detail": rejection.detail,
                    "job_id": request.job_id,
                    "transport_job_id": rejection.transport_job_id,
                }
        if len(record["safety_rejections"]) != len(candidates):
            records.append(record)
            continue

        record["primary_status"] = "safety_blocked"
        record["status"] = "safety_blocked"
        try:
            fallback_cached = cached_inference_response(storage, flux_request)
        except RuntimeError as exc:
            record["status"] = "invalid"
            record["fallback_status"] = "invalid"
            record["error"] = str(exc)
        else:
            if fallback_cached is not None:
                record["status"] = "hit"
                record["fallback_status"] = "hit"
                record["matched_provider"] = "flux_schnell"
        records.append(record)
    return records


def main() -> None:
    args = parse_args()
    raw: Any = json.loads(args.frames.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Storyboard frames file must contain a JSON array.")
    frames = [StoryboardFrame.model_validate(item) for item in raw]
    storage = R2ObjectStorage.create(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    records = audit_keyframe_cache(
        frames,
        storage=storage,
        model_id=args.model,
        flux_model_id=args.flux_model,
        size=args.size,
    )
    hits = sum(item["status"] == "hit" for item in records)
    misses = sum(item["status"] == "miss" for item in records)
    safety_blocked = sum(item["status"] == "safety_blocked" for item in records)
    invalid = sum(item["status"] == "invalid" for item in records)
    print(
        f"Summary: hits={hits} misses={misses} safety_blocked={safety_blocked} "
        f"invalid={invalid} total={len(records)}; Salad queue submissions=0"
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"Audit JSON: {args.json_output.resolve()}")
    if invalid:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
