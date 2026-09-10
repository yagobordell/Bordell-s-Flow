from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from ai_video_factory.compositor import (
    CompositionPlan,
    prepare_remotion_props,
    validate_remotion_visual,
)
from ai_video_factory.config import settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage Phase 9 media, render the frame-exact Remotion visual, and validate it."
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=settings.output_dir / "phase9" / "composition_plan.json",
        help="Validated Phase 9.2 composition_plan.json file.",
    )
    parser.add_argument(
        "--renderer-dir",
        type=Path,
        default=Path("remotion"),
        help="Local Remotion project directory.",
    )
    parser.add_argument(
        "--props-output",
        type=Path,
        default=settings.output_dir / "phase9" / "remotion_props.json",
        help="Renderer-only props JSON written before rendering.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase9" / "visual.mp4",
        help="Silent Phase 9.3 visual MP4 output.",
    )
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare media and props without invoking Remotion.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 <= args.crf <= 51:
        raise SystemExit("--crf must be between 0 and 51")

    plan = _read_plan(args.plan)
    renderer_dir = args.renderer_dir.resolve()
    package_json = renderer_dir / "package.json"
    if not package_json.is_file():
        raise SystemExit(f"Remotion project not found: {package_json}")

    props = prepare_remotion_props(plan, public_dir=renderer_dir / "public")
    args.props_output.parent.mkdir(parents=True, exist_ok=True)
    args.props_output.write_text(
        json.dumps(props.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    word_count = sum(len(caption.words) for caption in props.captions)
    print(f"Phase 9.3 Remotion props: {args.props_output.resolve()}")
    print(
        f"shots={len(props.shots)} captions={len(props.captions)} words={word_count} "
        f"presentation_normalizations={len(props.presentation_normalizations)}"
    )
    for item in props.presentation_normalizations:
        print(
            f"normalized word_id={item.word_id} "
            f"{item.source_text!r} -> {item.display_text!r}"
        )

    if args.prepare_only:
        print("Phase 9.3 Remotion staging and renderer props: OK")
        return

    remotion_cli = _resolve_remotion_cli(renderer_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(remotion_cli),
            "render",
            "src/index.ts",
            "Phase9Visual",
            str(args.output.resolve()),
            "--props",
            str(args.props_output.resolve()),
            "--codec",
            "h264",
            "--pixel-format",
            "yuv420p",
            "--crf",
            str(args.crf),
            "--muted",
            "--overwrite",
        ],
        cwd=renderer_dir,
        check=True,
    )

    media = validate_remotion_visual(args.output, props)
    frame_count = media.frame_count
    if frame_count is None:
        frame_count = round(media.duration_seconds * props.fps)

    print(f"Phase 9.3 visual render: {args.output.resolve()}")
    print(
        f"frames={frame_count} duration={media.duration_seconds:.3f}s "
        f"fps={media.fps:g} audio_streams={media.audio_stream_count}"
    )
    print("Phase 9.3 Remotion visual renderer: OK")


def _read_plan(path: Path) -> CompositionPlan:
    if not path.is_file():
        raise SystemExit(f"Required composition plan not found: {path}")
    return CompositionPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _resolve_remotion_cli(renderer_dir: Path) -> Path:
    binary_name = "remotion.cmd" if __import__("os").name == "nt" else "remotion"
    binary = renderer_dir / "node_modules" / ".bin" / binary_name
    if not binary.is_file():
        raise SystemExit(
            "Remotion CLI is not installed. From the repository root run: "
            "Push-Location remotion; npm.cmd install; Pop-Location"
        )
    return binary.resolve()


if __name__ == "__main__":
    main()
