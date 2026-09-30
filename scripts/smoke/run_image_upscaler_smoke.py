from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from ai_video_factory.config import settings
from ai_video_factory.providers.optium_image_upscaler import create_optium_image_upscaler


def _required(name: str, value: str | None) -> str:
    if value is None or not value.strip():
        raise SystemExit(f"{name} is missing. Add it to your local .env file.")
    return value.strip()


def _source_image(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (24, 32, 48))
    draw = ImageDraw.Draw(image)
    step = max(8, min(width, height) // 12)
    for offset in range(0, max(width, height), step):
        draw.line((0, offset, width, max(0, height - offset)), width=max(1, step // 8))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _validate_output(payload: bytes, *, width: int, height: int) -> None:
    with Image.open(BytesIO(payload)) as image:
        image.load()
        if image.size != (width, height):
            raise RuntimeError(
                f"Smoke output has wrong dimensions: expected {width}x{height}, "
                f"got {image.size[0]}x{image.size[1]}"
            )
        if image.format != "PNG":
            raise RuntimeError(f"Smoke output must be PNG, got {image.format}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real OptiumLabs image-upscaler inference through Salad Gateway and R2."
    )
    parser.add_argument(
        "--case",
        choices=("small", "real", "all"),
        default="all",
        help="small=640x360->1920x1080; real=1280x720->3840x2160; all runs both in order",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    provider = create_optium_image_upscaler(
        base_url=_required("IMAGE_UPSCALER_URL", settings.image_upscaler_url),
        salad_api_key=_required("SALAD_API_KEY", settings.salad_api_key),
        r2_endpoint_url=_required("R2_ENDPOINT_URL", settings.r2_endpoint_url),
        r2_bucket=_required("R2_BUCKET", settings.r2_bucket),
        r2_access_key_id=_required("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
        r2_secret_access_key=_required("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
        temp_dir=Path(settings.temp_dir) / "image-upscaler-smoke",
    )

    cases = {
        "small": (640, 360, 1920, 1080),
        "real": (1280, 720, 3840, 2160),
    }
    selected = ("small", "real") if args.case == "all" else (args.case,)
    for name in selected:
        source_width, source_height, target_width, target_height = cases[name]
        source = _source_image(source_width, source_height)
        result = provider.upscale(
            source,
            target_width=target_width,
            target_height=target_height,
        )
        _validate_output(result, width=target_width, height=target_height)
        print(
            f"PASS {name}: {source_width}x{source_height} -> "
            f"{target_width}x{target_height}; output_bytes={len(result)}"
        )


if __name__ == "__main__":
    main()
