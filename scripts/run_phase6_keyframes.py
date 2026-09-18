import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.domain import Shot, StoryboardFrame
from ai_video_factory.providers import (
    SafetyFallbackImageProvider,
    SaladFlux2KleinImageProvider,
    SaladIdeogramImageProvider,
)
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.flux2_klein import FLUX2_KLEIN_KEYFRAME_TASK
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_KEYFRAME_TASK
from ai_video_factory.workflows.storyboard_keyframes import generate_storyboard_keyframes

DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS = 300.0
DEFAULT_FLUX_PENDING_TIMEOUT_SECONDS = 1800.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Phase 6 keyframes with Ideogram 4 and FLUX safety fallback."
    )
    parser.add_argument(
        "--frames",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_frames.json",
    )
    parser.add_argument(
        "--shots",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
    )
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--fallback-model", default=settings.flux2_klein_model)
    parser.add_argument("--size", default="1024x1536")
    parser.add_argument("--quality", choices=("high", "auto"), default="high")
    parser.add_argument("--queue-name", default=settings.salad_ideogram4_queue_name)
    parser.add_argument(
        "--fallback-queue-name",
        default=settings.salad_flux2_klein_queue_name,
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
        "--pending-timeout-seconds",
        type=float,
        default=DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS,
        help="Maximum seconds for an already-prewarmed Ideogram worker to claim a queued job.",
    )
    parser.add_argument(
        "--fallback-pending-timeout-seconds",
        type=float,
        default=DEFAULT_FLUX_PENDING_TIMEOUT_SECONDS,
        help="Maximum seconds for a cold FLUX fallback worker to claim a queued job.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_keyframes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_keyframes.json",
    )
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _queue(name: str) -> SaladJobQueueClient:
    return SaladJobQueueClient(
        organization=_required_setting("SALAD_ORGANIZATION", settings.salad_organization),
        project=_required_setting("SALAD_PROJECT", settings.salad_project),
        queue_name=name,
        api_key=_required_setting("SALAD_API_KEY", settings.salad_api_key),
    )


async def main() -> None:
    args = parse_args()
    frames = _read_models(args.frames, StoryboardFrame)
    shots = _read_models(args.shots, Shot)

    storage = create_r2_storage(
        endpoint_url=_required_setting("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required_setting("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required_setting("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required_setting("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    ideogram_executor = InferenceJobExecutor(
        queue=_queue(args.queue_name),
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    flux_executor = InferenceJobExecutor(
        queue=_queue(args.fallback_queue_name),
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        pending_timeout_seconds=args.fallback_pending_timeout_seconds,
    )
    primary = SaladIdeogramImageProvider(
        executor=ideogram_executor,
        temp_dir=settings.temp_dir / "ideogram4-keyframe-client",
        task_name=IDEOGRAM4_KEYFRAME_TASK,
    )
    fallback = SaladFlux2KleinImageProvider(
        executor=flux_executor,
        temp_dir=settings.temp_dir / "flux2-klein-keyframe-client",
        task_name=FLUX2_KLEIN_KEYFRAME_TASK,
    )
    image_provider = SafetyFallbackImageProvider(
        primary=primary,
        fallback=fallback,
        fallback_model=args.fallback_model,
    )

    keyframes = await generate_storyboard_keyframes(
        frames,
        shots,
        image_provider=image_provider,
        output_dir=args.output_dir,
        model=args.model,
        size=args.size,
        quality=args.quality,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([keyframe.model_dump() for keyframe in keyframes], indent=2),
        encoding="utf-8",
    )
    print(f"Phase 6 storyboard keyframes complete. Metadata: {args.output.resolve()}")
    print(f"Generated {len(keyframes)} keyframe PNG files with Ideogram/FLUX fallback")


def _read_models[ModelT: BaseModel](
    path: Path,
    model_type: type[ModelT],
) -> list[ModelT]:
    if not path.is_file():
        raise SystemExit(f"Required JSON file not found: {path}")
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"JSON file must contain an array: {path}")
    return [model_type.model_validate(item) for item in raw]


if __name__ == "__main__":
    asyncio.run(main())
