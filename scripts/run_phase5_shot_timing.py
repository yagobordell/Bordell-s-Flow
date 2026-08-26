import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ai_video_factory.config import settings
from ai_video_factory.domain import BeatTiming, Shot
from ai_video_factory.workflows.shot_timing import build_shot_timings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive deterministic shot timing from Phase 3 shots and timed beats."
    )
    parser.add_argument(
        "--shots",
        type=Path,
        default=settings.output_dir / "phase3" / "shots.json",
        help="Phase 3 shots.json file.",
    )
    parser.add_argument(
        "--beat-timings",
        type=Path,
        default=settings.output_dir / "phase5" / "beat_timings.json",
        help="Phase 5 beat_timings.json file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase5" / "shot_timings.json",
        help="Path where ShotTiming entries will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    shots = _read_models(args.shots, Shot)
    beat_timings = _read_models(args.beat_timings, BeatTiming)
    shot_timings = build_shot_timings(shots, beat_timings)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [timing.model_dump() for timing in shot_timings]
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Phase 5 shot timing complete. Artifact: {args.output.resolve()}")
    print(
        f"Derived {len(shot_timings)} shots across "
        f"{shot_timings[-1].end_seconds:.3f} seconds"
    )


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
