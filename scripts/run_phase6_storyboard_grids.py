import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.config import settings
from ai_video_factory.domain import Scene, Shot, StoryboardKeyframe
from ai_video_factory.workflows.storyboard_grids import build_storyboard_grids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compose deterministic scene-level storyboard grids from generated keyframes."
    )
    parser.add_argument(
        "--scenes",
        type=Path,
        default=settings.output_dir / "phase2" / "scenes.json",
    )
    parser.add_argument(
        "--shots",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
    )
    parser.add_argument(
        "--keyframes",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_keyframes.json",
    )
    parser.add_argument(
        "--thumbnail-width",
        type=int,
        default=320,
    )
    parser.add_argument(
        "--thumbnail-height",
        type=int,
        default=480,
    )
    parser.add_argument(
        "--max-columns",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_grids",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase6" / "storyboard_grids.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    scenes = _read_models(args.scenes, Scene)
    shots = _read_models(args.shots, Shot)
    keyframes = _read_models(args.keyframes, StoryboardKeyframe)

    grids = build_storyboard_grids(
        scenes,
        shots,
        keyframes,
        keyframe_root=args.keyframes.parent,
        output_dir=args.output_dir,
        thumbnail_width=args.thumbnail_width,
        thumbnail_height=args.thumbnail_height,
        max_columns=args.max_columns,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([grid.model_dump() for grid in grids], indent=2),
        encoding="utf-8",
    )

    print(f"Phase 6 storyboard grids complete. Metadata: {args.output.resolve()}")
    print(f"Composed {len(grids)} scene grid PNG files in {args.output_dir.resolve()}")


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
