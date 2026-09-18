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
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_KEYFRAME_TASK,
    FLUX2_KLEIN_MODEL_ID,
)
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_KEYFRAME_TASK

DEFAULT_SIZE = "1024x1536"
_LEGACY_CACHE_ONLY_VARIANT = "safe_fallback"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Phase 6 keyframe cache and safety-fallback state in R2 "
            "without submitting Salad work."
        )
    )
    parser.add_argument("frames_file", type=Path)
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--fallback-model", default=settings.flux2_klein_model)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _load_frames(path: Path) -> list[StoryboardFrame]:
    if not path.is_file():
        raise SystemExit(f"Storyboard frames file not found: {path}")
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Storyboard frames file must contain a JSON array.")
    frames = [StoryboardFrame.model_validate(item) for item in raw]
    shot_ids = [frame.shot_id for frame in frames]
    if len(shot_ids) != len(set(shot_ids)):
        raise SystemExit("Storyboard frame shot IDs must be unique.")
    return frames


def _cache_status(storage: R2ObjectStorage, request: Any) -> tuple[str, str | None]:
    try:
        response = cached_inference_response(storage, request)
    except RuntimeError as exc:
        return "invalid", str(exc)
    return ("hit", None) if response is not None else ("miss", None)


def audit_keyframe_cache(
    frames: list[StoryboardFrame],
    *,
    storage: R2ObjectStorage,
    model_id: str,
    fallback_model_id: str,
    size: str,
) -> list[dict[str, Any]]:
    width, height = parse_ideogram_size(size)
    records: list[dict[str, Any]] = []

    for frame in frames:
        candidates = [
            (
                variant_name,
                build_ideogram_job_request(
                    task_name=IDEOGRAM4_KEYFRAME_TASK,
                    caption=caption,
                    model_id=model_id,
                    width=width,
                    height=height,
                ),
            )
            for variant_name, caption in reference_caption_variants(
                frame.prompt,
                task_name=IDEOGRAM4_KEYFRAME_TASK,
            )
        ]
        canonical = candidates[0][1]
        record: dict[str, Any] = {
            "shot_id": frame.shot_id,
            "status": "miss",
            "provider": "ideogram4",
            "matched_variant": None,
            "job_id": canonical.job_id,
            "object_key": canonical.output.key,
            "expected_request_sha256": canonical.fingerprint(),
            "safety_rejected_variants": [],
            "error": None,
        }

        for variant_name, request in candidates:
            status, error = _cache_status(storage, request)
            if status == "invalid":
                record.update(
                    status="invalid",
                    matched_variant=variant_name,
                    job_id=request.job_id,
                    object_key=request.output.key,
                    expected_request_sha256=request.fingerprint(),
                    error=error,
                )
                break
            if status == "hit":
                record.update(
                    status="hit",
                    matched_variant=variant_name,
                    job_id=request.job_id,
                    object_key=request.output.key,
                    expected_request_sha256=request.fingerprint(),
                )
                break
        if record["status"] in {"hit", "invalid"}:
            records.append(record)
            continue

        executable = [
            (variant_name, request)
            for variant_name, request in candidates
            if variant_name != _LEGACY_CACHE_ONLY_VARIANT
        ]
        rejected = [
            variant_name
            for variant_name, request in executable
            if find_safety_rejection(storage, request) is not None
        ]
        record["safety_rejected_variants"] = rejected
        if len(rejected) != len(executable):
            records.append(record)
            continue

        fallback_request = build_flux_job_request(
            task_name=FLUX2_KLEIN_KEYFRAME_TASK,
            prompt=frame.prompt,
            model_id=fallback_model_id,
            width=width,
            height=height,
        )
        status, error = _cache_status(storage, fallback_request)
        record.update(
            provider="flux2_klein",
            status="hit" if status == "hit" else ("invalid" if status == "invalid" else "safety_blocked"),
            matched_variant="flux2_klein",
            job_id=fallback_request.job_id,
            object_key=fallback_request.output.key,
            expected_request_sha256=fallback_request.fingerprint(),
            error=error,
        )
        records.append(record)

    return records


def main() -> None:
    args = parse_args()
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
        _load_frames(args.frames_file),
        storage=storage,
        model_id=args.model,
        fallback_model_id=args.fallback_model,
        size=args.size,
    )
    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    safety = sum(record["status"] == "safety_blocked" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    print(
        f"Phase 6 cache: hits={hits} misses={misses} safety_blocked={safety} "
        f"invalid={invalid} total={len(records)} Salad submissions=0"
    )
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Audit JSON: {args.json_output.resolve()}")
    if invalid:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
