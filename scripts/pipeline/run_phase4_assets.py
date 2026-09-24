import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.providers import SaladQwenImage21Provider
from ai_video_factory.providers.images import parse_image_size
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.postgres_queue import PostgresJobQueueClient
from ai_video_factory.providers.r2 import create_r2_storage
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_PRODUCTION_SIZE,
    QWEN_IMAGE_21_REFERENCE_TASK,
)
from ai_video_factory.workflows.reference_assets import generate_reference_assets

DEFAULT_SIZE = QWEN_IMAGE_21_PRODUCTION_SIZE
DEFAULT_QUALITY = "high"
DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS = 1800.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate canonical Phase 4 references with Qwen-Image-2.1 on Salad."
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
    )
    parser.add_argument("--model", default=settings.qwen_image_21_model)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--quality", choices=("high", "auto"), default=DEFAULT_QUALITY)
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
        default=DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS,
        help="Maximum seconds for a cold Qwen worker to claim a queued job.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets.json",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _queue() -> PostgresJobQueueClient:
    return PostgresJobQueueClient(
        dsn=_required_setting("POSTGRES_DSN", settings.postgres_dsn),
    )


async def main() -> None:
    args = parse_args()
    width, height = parse_image_size(args.size)
    if f"{width}x{height}" != QWEN_IMAGE_21_PRODUCTION_SIZE:
        raise SystemExit(
            f"Phase 4 production references must be {QWEN_IMAGE_21_PRODUCTION_SIZE}; "
            f"received {width}x{height}"
        )
    if not args.references_file.is_file():
        raise SystemExit(f"Visual references file not found: {args.references_file}")

    raw: Any = json.loads(args.references_file.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise SystemExit("Visual references file must contain a JSON array.")
    references = [VisualReference.model_validate(item) for item in raw]

    storage = create_r2_storage(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    executor = InferenceJobExecutor(
        queue=_queue(),
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    provider = SaladQwenImage21Provider(
        executor=executor,
        temp_dir=settings.temp_dir / "qwen-image-21-reference-client",
        task_name=QWEN_IMAGE_21_REFERENCE_TASK,
    )

    assets = await generate_reference_assets(
        references,
        image_provider=provider,
        output_dir=args.output_dir,
        model=args.model,
        size=args.size,
        quality=args.quality,
    )

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(
        json.dumps([asset.model_dump() for asset in assets], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Phase 4 reference assets complete. Metadata: {args.metadata.resolve()}")
    print(f"Generated {len(assets)} reference PNG files with Qwen-Image-2.1")


if __name__ == "__main__":
    asyncio.run(main())
