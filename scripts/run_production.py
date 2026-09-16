from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.workflows.production_runner import (
    PRODUCTION_STAGE_NAMES,
    ProductionRunner,
    ProductionStage,
    ProductionStageBlocked,
    SubprocessStageExecutor,
    build_production_stages,
)


class OptimizedGpuStageExecutor:
    """Use ready-before-queue Salad wrappers for GPU-backed production stages."""

    def __init__(self, *, repo_root: Path, output_dir: Path) -> None:
        self._repo_root = repo_root
        self._output_dir = output_dir
        self._default = SubprocessStageExecutor(repo_root=repo_root)

    def __call__(self, stage: ProductionStage) -> None:
        arguments = self._controlled_arguments(stage.name)
        if arguments is None:
            self._default(stage)
            return

        executable = "powershell.exe" if os.name == "nt" else "pwsh"
        command = [executable, "-NoProfile"]
        if os.name == "nt":
            command.extend(["-ExecutionPolicy", "Bypass"])
        command.extend(["-File", *arguments])
        subprocess.run(command, cwd=self._repo_root, check=True)

    def _controlled_arguments(self, stage_name: str) -> list[str] | None:
        output = self._output_dir
        if stage_name == "phase4-reference-assets":
            return [
                "scripts/run_phase4_assets_controlled.ps1",
                "-ReferencesFile",
                str(output / "phase4" / "visual_references.json"),
                "-OutputDir",
                str(output / "phase4" / "reference_assets"),
                "-Metadata",
                str(output / "phase4" / "reference_assets.json"),
                "-NonInteractive",
            ]
        if stage_name == "phase5-narration":
            return [
                "scripts/run_phase5_audio_controlled.ps1",
                "-SourceFile",
                str(output / "phase2" / "source_script.json"),
                "-OutputDir",
                str(output / "phase5"),
                "-Metadata",
                str(output / "phase5" / "narration.json"),
                "-NonInteractive",
            ]
        if stage_name == "phase5-alignment":
            return [
                "scripts/run_phase5_alignment_controlled.ps1",
                "-Source",
                str(output / "phase2" / "source_script.json"),
                "-Narration",
                str(output / "phase5" / "narration.json"),
                "-Audio",
                str(output / "phase5" / "narration.wav"),
                "-Output",
                str(output / "phase5" / "narration_words.json"),
                "-NonInteractive",
            ]
        if stage_name == "phase6-keyframes":
            return [
                "scripts/run_phase6_keyframes_controlled.ps1",
                "-Frames",
                str(output / "phase6" / "storyboard_frames.json"),
                "-Shots",
                str(output / "phase3" / "shots.json"),
                "-OutputDir",
                str(output / "phase6" / "storyboard_keyframes"),
                "-Output",
                str(output / "phase6" / "storyboard_keyframes.json"),
                "-NonInteractive",
            ]
        if stage_name == "phase8-videos":
            return [
                "scripts/run_phase8_videos_controlled.ps1",
                "-Keyframes",
                str(output / "phase6" / "storyboard_keyframes.json"),
                "-Prompts",
                str(output / "phase8" / "video_prompts.json"),
                "-Timings",
                str(output / "phase5" / "shot_timings.json"),
                "-OutputDir",
                str(output / "phase8"),
                "-NonInteractive",
            ]
        return None


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
        executor=OptimizedGpuStageExecutor(
            repo_root=Path("."),
            output_dir=args.output_dir,
        ),
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
