from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.compositor import build_composition_plan
from ai_video_factory.config import settings
from ai_video_factory.domain import ShotTiming, VideoClip


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Phase 8 clips and build the frame-exact Phase 9 composition plan."
    )
    parser.add_argument(
        "--clips",
        type=Path,
        default=settings.output_dir / "phase8" / "video_clips.json",
        help="Phase 8 video_clips.json file.",
    )
    parser.add_argument(
        "--timings",
        type=Path,
        default=settings.output_dir / "phase5" / "shot_timings.json",
        help="Phase 5 shot_timings.json file.",
    )
    parser.add_argument(
        "--clip-base-dir",
        type=Path,
        default=None,
        help="Base directory used to resolve VideoClip.uri. Defaults to the clips JSON directory.",
    )
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase9" / "composition_plan.json",
        help="Path where the validated composition plan will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    clips = _read_models(args.clips, VideoClip)
    timings = _read_models(args.timings, ShotTiming)
    clip_base_dir = args.clip_base_dir or args.clips.parent

    plan = build_composition_plan(
        clips,
        timings,
        clip_base_dir=clip_base_dir,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Phase 9.1 composition plan: {args.output.resolve()}")
    print(
        f"shots={len(plan.shots)} frames={plan.total_frames} "
        f"duration={plan.total_frames / plan.fps:.3f}s fps={plan.fps}"
    )
    print("Phase 9.1 media probe and frame-exact timeline: OK")


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
    main()
