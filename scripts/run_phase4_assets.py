import argparse
import asyncio
import json
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.domain import VisualReference
from ai_video_factory.providers import (
    SafetyFallbackImageProvider,
    SaladFlux2KleinImageProvider,
    SaladIdeogramImageProvider,
)
from ai_video_factory.providers.images import parse_image_size
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.flux2_klein import FLUX2_KLEIN_REFERENCE_TASK
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_REFERENCE_TASK
from ai_video_factory.workflows.reference_assets import generate_reference_assets

DEFAULT_SIZE = "1536x864"
DEFAULT_QUALITY = "high"
DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS = 300.0
DEFAULT_FLUX_PENDING_TIMEOUT_SECONDS = 1800.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate canonical Phase 4 references with Ideogram 4 and FLUX.2 Klein 4B "
            "as a safety-only fallback."
        )
    )
    parser.add_argument(
        "references_file",
        type=Path,
        nargs="?",
        default=settings.output_dir / "phase4" / "visual_references.json",
    )
    parser.add_argument("--model", default=settings.ideogram4_model)
    parser.add_argument("--fallback-model", default=settings.flux2_klein_model)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument(
        "--quality",
        choices=("high", "auto"),
        default=DEFAULT_QUALITY,
    )
    parser.add_argument(
        "--queue-name",
        default=settings.salad_ideogram4_queue_name,
    )
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
        "--prewarm-fallback-on-demand",
        action="store_true",
        help="Prewarm FLUX only after the first terminal Ideogram safety rejection.",
    )
    parser.add_argument(
        "--fallback-prewarm-timeout-minutes",
        type=int,
        default=60,
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


def _queue(name: str) -> SaladJobQueueClient:
    return SaladJobQueueClient(
        organization=_required_setting("SALAD_ORGANIZATION", settings.salad_organization),
        project=_required_setting("SALAD_PROJECT", settings.salad_project),
        queue_name=name,
        api_key=_required_setting("SALAD_API_KEY", settings.salad_api_key),
    )


async def _prewarm_flux_fallback(timeout_minutes: int) -> None:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        raise RuntimeError("PowerShell is required for on-demand FLUX prewarm.")
    script = Path(__file__).with_name("start_salad_flux_prewarm.ps1")
    command = [
        executable,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-TimeoutMinutes",
        str(timeout_minutes),
        "-NonInteractive",
    ]
    print("Ideogram safety rejection confirmed; prewarming FLUX before fallback submission.")
    await asyncio.to_thread(subprocess.run, command, check=True)


async def main() -> None:
    args = parse_args()
    width, height = parse_image_size(args.size)
    if width * 9 != height * 16:
        raise SystemExit(
            f"Phase 4 production references must be exact 16:9; received {width}x{height}"
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
        temp_dir=settings.temp_dir / "ideogram4-reference-client",
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )
    fallback = SaladFlux2KleinImageProvider(
        executor=flux_executor,
        temp_dir=settings.temp_dir / "flux2-klein-reference-client",
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )
    before_fallback = (
        partial(_prewarm_flux_fallback, args.fallback_prewarm_timeout_minutes)
        if args.prewarm_fallback_on_demand
        else None
    )

    provider = SafetyFallbackImageProvider(
        primary=primary,
        fallback=fallback,
        before_fallback=before_fallback,
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
    print(f"Generated {len(assets)} reference PNG files with Ideogram/FLUX safety fallback")


if __name__ == "__main__":
    asyncio.run(main())
