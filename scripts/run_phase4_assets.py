import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.storage import R2ObjectStorage
from ai_video_factory.providers import SaladIdeogramImageProvider
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_REFERENCE_TASK
from ai_video_factory.workflows.reference_assets import generate_reference_assets

DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "high"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate canonical Phase 4 references with the Salad Ideogram 4 worker."
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
        help="Phase 4 visual_references.json file.",
    )
    parser.add_argument(
        "--model",
        default=settings.ideogram4_model,
        help="Ideogram 4 NF4 model hosted by the dedicated Salad worker.",
    )
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        help="Generated image size, for example 1024x1024.",
    )
    parser.add_argument(
        "--quality",
        choices=("high", "auto"),
        default=DEFAULT_QUALITY,
        help="Ideogram worker is fixed to the V4_QUALITY_48 preset.",
    )
    parser.add_argument(
        "--queue-name",
        default=settings.salad_ideogram4_queue_name,
        help="Dedicated Salad queue shared by Ideogram reference and keyframe jobs.",
    )
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
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets",
        help="Directory where generated PNG reference assets will be written.",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=settings.output_dir / "phase4" / "reference_assets.json",
        help="Path where ReferenceAsset metadata will be written.",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


async def main() -> None:
    args = parse_args()

    if not args.references_file.is_file():
        raise SystemExit(f"Visual references file not found: {args.references_file}")

    raw: Any = json.loads(args.references_file.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise SystemExit("Visual references file must contain a JSON array.")

    references = [VisualReference.model_validate(item) for item in raw]
    storage = R2ObjectStorage.create(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting(
            "R2_SECRET_ACCESS_KEY",
            settings.r2_secret_access_key,
        ),
    )
    queue = SaladJobQueueClient(
        organization=_required_setting("SALAD_ORGANIZATION", settings.salad_organization),
        project=_required_setting("SALAD_PROJECT", settings.salad_project),
        queue_name=args.queue_name,
        api_key=_required_setting("SALAD_API_KEY", settings.salad_api_key),
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    provider = SaladIdeogramImageProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "ideogram4-reference-client",
        task_name=IDEOGRAM4_REFERENCE_TASK,
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
    payload = [asset.model_dump() for asset in assets]
    args.metadata.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 4 reference assets complete. Metadata: {args.metadata.resolve()}")
    print(f"Generated {len(assets)} reference PNG files via Salad queue {args.queue_name}")


if __name__ == "__main__":
    asyncio.run(main())
