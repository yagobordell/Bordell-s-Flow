from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.workflows.production_runner import (
    PRODUCTION_STAGE_NAMES,
    ProductionRunner,
    ProductionStageBlocked,
    build_production_stages,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the production pipeline resumably, adopting valid persisted artifacts and "
            "executing only pending or stale stages."
        )
    )
    parser.add_argument(
        "script_file",
        type=Path,
        nargs="?",
        default=Path("data/input/script.txt"),
        help="UTF-8 source script used as the canonical production input.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir,
        help="Shared production output root.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Operational resume manifest. Defaults to <output-dir>/production_manifest.json.",
    )
    parser.add_argument(
        "--through",
        choices=PRODUCTION_STAGE_NAMES,
        default=None,
        help="Stop after the selected stage instead of running through Phase 8 video generation.",
    )
    parser.add_argument(
        "--force-stage",
        action="append",
        choices=PRODUCTION_STAGE_NAMES,
        default=[],
        help="Rerun one stage even when its recorded fingerprints are current.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Inspect current stage state without executing or adopting anything.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.script_file.is_file():
        raise SystemExit(f"Production script file not found: {args.script_file}")

    manifest_path = args.manifest or args.output_dir / "production_manifest.json"
    stages = build_production_stages(
        script_file=args.script_file,
        output_dir=args.output_dir,
    )
    runner = ProductionRunner(
        stages,
        manifest_path=manifest_path,
        repo_root=Path("."),
    )

    if args.plan:
        print(f"Production manifest: {manifest_path}")
        for inspection in runner.plan(through=args.through):
            print(
                f"{inspection.status.upper():9} "
                f"{inspection.stage.name}: {inspection.reason}"
            )
        return

    try:
        summary = runner.run(
            through=args.through,
            force_stages=set(args.force_stage),
        )
    except ProductionStageBlocked as exc:
        print(f"BLOCK {exc.stage.name}: {exc}", file=sys.stderr)
        raise SystemExit(21) from exc

    print(f"Production manifest: {manifest_path.resolve()}")
    print(
        f"executed={len(summary.executed)} adopted={len(summary.adopted)} "
        f"skipped={len(summary.skipped)}"
    )
    if args.through is None or args.through == "phase8-videos":
        print("Production phases 2-8 are complete. The local renderer can now run Phase 9.")


if __name__ == "__main__":
    main()
