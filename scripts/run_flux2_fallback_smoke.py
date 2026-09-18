from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from r2_client import create_r2_storage

from ai_video_factory.config import settings
from ai_video_factory.providers import SafetyFallbackImageProvider, SaladFlux2KleinImageProvider
from ai_video_factory.providers.inference_jobs import (
    InferenceJobExecutor,
    RemoteInferenceRejectedError,
)
from ai_video_factory.providers.salad_queue import SaladJobQueueClient
from ai_video_factory.workers.flux2_klein import FLUX2_KLEIN_MODEL_ID, FLUX2_KLEIN_REFERENCE_TASK


class TerminalSafetyPrimary:
    """Deterministic primary stub for exercising only the real remote fallback path."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        size: str,
        quality: str,
        output_format: str,
    ):
        del prompt, model, size, quality, output_format
        self.calls += 1
        raise RemoteInferenceRejectedError(
            "flux2-fallback-smoke-primary",
            "Ideogram 4 safety filter blocked all provider caption variants",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the real Salad FLUX.2 Klein fallback after one deterministic terminal "
            "Ideogram safety rejection at the provider boundary."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output/deployment-validation"),
    )
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument(
        "--prompt",
        default=(
            "Cinematic documentary still of a compact robotic cinema camera on a clean studio "
            "table, realistic materials, soft neutral directional lighting, no text or logos."
        ),
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
    return parser.parse_args()


def _required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to .env before running the fallback smoke.")
    return value.strip()


async def main() -> None:
    args = parse_args()
    storage = create_r2_storage(
        endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        bucket=_required("R2_BUCKET", settings.r2_bucket),
        access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        secret_access_key=_required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
    )
    queue = SaladJobQueueClient(
        organization=_required("SALAD_ORGANIZATION", settings.salad_organization),
        project=_required("SALAD_PROJECT", settings.salad_project),
        queue_name=settings.salad_flux2_klein_queue_name,
        api_key=_required("SALAD_API_KEY", settings.salad_api_key),
    )
    executor = InferenceJobExecutor(
        queue=queue,
        storage=storage,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    fallback = SaladFlux2KleinImageProvider(
        executor=executor,
        temp_dir=settings.temp_dir / "flux2-fallback-smoke-client",
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )
    primary = TerminalSafetyPrimary()
    provider = SafetyFallbackImageProvider(
        primary=primary,
        fallback=fallback,
        fallback_model=FLUX2_KLEIN_MODEL_ID,
    )

    image = await provider.generate_image(
        prompt=args.prompt,
        model=settings.ideogram4_model,
        size=args.size,
        quality="high",
        output_format="png",
    )
    if primary.calls != 1:
        raise RuntimeError(
            f"Primary provider was called {primary.calls} times; expected exactly one"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    png_path = args.output_dir / "flux2-fallback-e2e.png"
    png_path.write_bytes(image.content)
    if len(image.content) <= 8 or not image.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Fallback smoke did not produce a valid PNG signature")

    report = {
        "status": "succeeded",
        "primary_calls": primary.calls,
        "fallback_model": FLUX2_KLEIN_MODEL_ID,
        "size": args.size,
        "png": png_path.as_posix(),
        "size_bytes": len(image.content),
        "metadata": dict(image.metadata),
    }
    report_path = args.output_dir / "flux2-fallback-e2e.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Fallback E2E smoke report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
