from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from ai_video_factory.compositor import (
    CompositionPlan,
    RemotionVisualProfile,
    prepare_remotion_props,
    validate_remotion_visual,
)
from ai_video_factory.config import settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Phase 9.4 boundary-preserving transitions and motion overlays."
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=settings.output_dir / "phase9" / "composition_plan.json",
    )
    parser.add_argument("--renderer-dir", type=Path, default=Path("remotion"))
    parser.add_argument(
        "--props-output",
        type=Path,
        default=settings.output_dir / "phase9" / "remotion_props_9_4.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase9" / "visual_motion.mp4",
    )
    parser.add_argument("--transition-frames", type=int, default=6)
    parser.add_argument("--transition-floor-opacity", type=float, default=0.72)
    parser.add_argument("--transition-scale", type=float, default=1.015)
    parser.add_argument("--caption-motion-frames", type=int, default=4)
    parser.add_argument("--boundary-accent-frames", type=int, default=5)
    parser.add_argument(
        "--show-captions",
        action="store_true",
        help="Opt in to rendering subtitles; disabled by default.",
    )
    parser.add_argument(
        "--show-progress-bar",
        action="store_true",
        help="Opt in to rendering the top progress bar; disabled by default.",
    )
    parser.add_argument("--no-progress-bar", action="store_true")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--prepare-only", action="store_true")
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

    profile = RemotionVisualProfile(
        transition_frames=args.transition_frames,
        transition_floor_opacity=args.transition_floor_opacity,
        transition_scale=args.transition_scale,
        caption_motion_frames=args.caption_motion_frames,
        boundary_accent_frames=args.boundary_accent_frames,
        show_captions=args.show_captions,
        show_progress_bar=args.show_progress_bar and not args.no_progress_bar,
    )
    props = prepare_remotion_props(
        plan,
        public_dir=renderer_dir / "public",
        visual_profile=profile,
    )

    args.props_output.parent.mkdir(parents=True, exist_ok=True)
    args.props_output.write_text(
        json.dumps(props.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    word_count = sum(len(caption.words) for caption in props.captions)
    print(f"Phase 9.4 Remotion props: {args.props_output.resolve()}")
    print(
        f"shots={len(props.shots)} captions={len(props.captions)} words={word_count} "
        f"frames={props.total_frames} fps={props.fps}"
    )
    print(
        "motion_profile="
        f"transition:{profile.transition_frames}f "
        f"caption:{profile.caption_motion_frames}f "
        f"accent:{profile.boundary_accent_frames}f "
        f"captions={profile.show_captions} "
        f"progress_bar={profile.show_progress_bar}"
    )

    if args.prepare_only:
        print("Phase 9.4 motion props and media staging: OK")
        return

    remotion_cli = _resolve_remotion_cli(renderer_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(remotion_cli),
            "render",
            "src/index.ts",
            "Phase9Motion",
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

    print(f"Phase 9.4 motion render: {args.output.resolve()}")
    print(
        f"frames={frame_count} duration={media.duration_seconds:.3f}s "
        f"fps={media.fps:g} audio_streams={media.audio_stream_count}"
    )
    print("Phase 9.4 transitions and motion overlays: OK")


def _read_plan(path: Path) -> CompositionPlan:
    if not path.is_file():
        raise SystemExit(f"Required composition plan not found: {path}")
    return CompositionPlan.model_validate_json(path.read_text(encoding="utf-8"))


def _resolve_remotion_cli(renderer_dir: Path) -> Path:
    binary_name = "remotion.cmd" if os.name == "nt" else "remotion"
    binary = renderer_dir / "node_modules" / ".bin" / binary_name
    if not binary.is_file():
        windows_hint = "Push-Location .\\remotion; npm.cmd install; Pop-Location"
        raise SystemExit(
            "Remotion CLI is not installed. Install the isolated renderer first. "
            f"Windows PowerShell: {windows_hint}"
        )
    return binary.resolve()


if __name__ == "__main__":
    main()
