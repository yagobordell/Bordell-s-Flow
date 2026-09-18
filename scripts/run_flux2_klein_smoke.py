from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
from pathlib import Path

from PIL import Image
from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.providers.salad_flux import SaladFlux2KleinImageProvider
from ai_video_factory.providers.inference_jobs import InferenceJobExecutor
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.flux2_klein import (
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_REFERENCE_TASK,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real FLUX.2 Klein fallback text-to-image job through Salad and R2."
    )
    parser.add_argument(
        "--prompt",
        default=(
            "A cinematic documentary photograph of a windswept lighthouse on a rocky Atlantic "
            "coast at blue hour, realistic natural textures, restrained color, no text."
        ),
    )
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument(
        "--queue-name",
        default=settings.salad_flux2_klein_queue_name,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "flux2-klein-smoke" / "image.png",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=settings.output_dir / "flux2-klein-smoke" / "report.json",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=settings.inference_client_poll_seconds,
    )
    parser.add_argument("--pending-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--timeout-seconds", type=float, default=1200.0)
    return parser.parse_args()


def _required_setting(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


async def main() -> None:
    args = parse_args()
    storage = create_r2_storage(
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
        pending_timeout_seconds=args.pending_timeout_seconds,
    )
    provider = SaladFlux2KleinImageProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "flux2-klein-smoke-client",
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )

    started = time.monotonic()
    image = await provider.generate_image(
        prompt=args.prompt,
        model=FLUX2_KLEIN_MODEL_ID,
        size=args.size,
        quality="high",
        output_format="png",
    )
    elapsed = time.monotonic() - started

    with Image.open(io.BytesIO(image.content)) as decoded:
        decoded.load()
        width, height = decoded.size
        image_format = decoded.format

    expected_width, expected_height = (int(value) for value in args.size.split("x", 1))
    if (width, height) != (expected_width, expected_height):
        raise RuntimeError(
            f"Smoke PNG dimensions {(width, height)} do not match requested "
            f"{(expected_width, expected_height)}"
        )
    if image_format != "PNG":
        raise RuntimeError(f"Smoke output is not PNG: {image_format!r}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image.content)
    report = {
        "elapsed_seconds": round(elapsed, 3),
        "width": width,
        "height": height,
        "png_size_bytes": len(image.content),
        "output": str(args.output.resolve()),
        "provider": image.metadata.get("provider"),
        "model": image.metadata.get("model"),
        "model_revision": image.metadata.get("model_revision"),
        "job_id": image.metadata.get("job_id"),
        "request_sha256": image.metadata.get("request_sha256"),
        "replayed": image.metadata.get("replayed"),
        "fallback_from": image.metadata.get("fallback_from"),
        "fallback_reason": image.metadata.get("fallback_reason"),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("FLUX2_KLEIN_SMOKE " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
