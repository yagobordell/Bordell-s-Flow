from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import StoryboardFrame
from ai_video_factory.inference.storage import R2ObjectStorage
from ai_video_factory.providers.images import parse_image_size
from ai_video_factory.providers.inference_jobs import cached_inference_response
from ai_video_factory.providers.salad_qwen_image import build_qwen_image_job_request
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
)

DEFAULT_SIZE = "1536x864"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Phase 6 Qwen-Image-2.1 keyframe cache entries in R2 "
            "without submitting Salad work."
        )
    )
    parser.add_argument("frames_file", type=Path)
    parser.add_argument("--model", default=settings.qwen_image_21_model)
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
    model_id: str = QWEN_IMAGE_21_MODEL_ID,
    size: str = DEFAULT_SIZE,
) -> list[dict[str, Any]]:
    width, height = parse_image_size(size)
    records: list[dict[str, Any]] = []

    for frame in frames:
        request = build_qwen_image_job_request(
            task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
            prompt=frame.prompt,
            model_id=model_id,
            width=width,
            height=height,
        )
        status, error = _cache_status(storage, request)
        records.append(
            {
                "shot_id": frame.shot_id,
                "status": status,
                "provider": "qwen_image_21",
                "matched_variant": "qwen_image_21" if status == "hit" else None,
                "job_id": request.job_id,
                "object_key": request.output.key,
                "expected_request_sha256": request.fingerprint(),
                "safety_rejected_variants": [],
                "error": error,
            }
        )

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
        size=args.size,
    )
    hits = sum(record["status"] == "hit" for record in records)
    misses = sum(record["status"] == "miss" for record in records)
    invalid = sum(record["status"] == "invalid" for record in records)
    print(
        f"Phase 6 cache: hits={hits} misses={misses} invalid={invalid} "
        f"total={len(records)} Salad submissions=0"
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
