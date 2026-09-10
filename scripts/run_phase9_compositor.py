from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.compositor import build_composition_plan
from ai_video_factory.config import settings
from ai_video_factory.domain import NarrationWord, ShotTiming, VideoClip


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the validated frame-exact Phase 9 composition and caption plan."
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
        "--words",
        type=Path,
        default=settings.output_dir / "phase5" / "narration_words.json",
        help="Phase 5 narration_words.json file.",
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
    parser.add_argument("--caption-max-words", type=int, default=5)
    parser.add_argument("--caption-max-chars", type=int, default=36)
    parser.add_argument("--caption-max-duration-seconds", type=float, default=2.5)
    parser.add_argument("--caption-pause-threshold-seconds", type=float, default=0.35)
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
    words = _read_models(args.words, NarrationWord)
    clip_base_dir = args.clip_base_dir or args.clips.parent

    plan = build_composition_plan(
        clips,
        timings,
        clip_base_dir=clip_base_dir,
        words=words,
        width=args.width,
        height=args.height,
        fps=args.fps,
        caption_max_words=args.caption_max_words,
        caption_max_chars=args.caption_max_chars,
        caption_max_duration_seconds=args.caption_max_duration_seconds,
        caption_pause_threshold_seconds=args.caption_pause_threshold_seconds,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    caption_word_count = sum(len(caption.words) for caption in plan.captions)
    print(f"Phase 9 composition plan: {args.output.resolve()}")
    print(
        f"shots={len(plan.shots)} frames={plan.total_frames} "
        f"duration={plan.total_frames / plan.fps:.3f}s fps={plan.fps}"
    )
    print(f"captions={len(plan.captions)} words={caption_word_count}")
    print("Phase 9.1 media probe and frame-exact timeline: OK")
    print("Phase 9.2 deterministic caption track: OK")


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
