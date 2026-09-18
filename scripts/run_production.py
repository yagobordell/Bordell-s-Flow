from __future__ import annotations

import argparse
import json
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

    def __init__(
        self,
        *,
        repo_root: Path,
        output_dir: Path,
        hold_shared_workers: bool = False,
    ) -> None:
        self._repo_root = repo_root
        self._output_dir = output_dir
        self._hold_shared_workers = hold_shared_workers
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
            arguments = [
                "scripts/run_phase4_assets_controlled.ps1",
                "-ReferencesFile",
                str(output / "phase4" / "visual_references.json"),
                "-OutputDir",
                str(output / "phase4" / "reference_assets"),
                "-Metadata",
                str(output / "phase4" / "reference_assets.json"),
                "-NonInteractive",
            ]
            if self._hold_shared_workers:
                arguments.append("-KeepIdeogramWarm")
            return arguments
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
            arguments = [
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
            if self._hold_shared_workers:
                arguments.append("-ReleaseSharedIdeogram")
            return arguments
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
    parser.add_argument(
        "--max-parallel-stages",
        type=int,
        default=4,
        help="Maximum concurrently executing DAG stages. Defaults to 4.",
    )
    parser.add_argument(
        "--max-parallel-gpu-stages",
        type=int,
        default=2,
        help="Maximum different GPU-backed stages active at once. Defaults to 2.",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Disable DAG concurrency for debugging and regression isolation.",
    )
    parser.add_argument(
        "--end-to-end",
        action="store_true",
        help=(
            "Enable whole-video lifecycle optimizations such as temporarily retaining "
            "a shared Ideogram worker until its final use. Requires outer cleanup."
        ),
    )
    parser.add_argument(
        "--metrics",
        type=Path,
        default=None,
        help="Write stage wall-clock metrics JSON. Defaults to <output-dir>/production_metrics.json.",
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
    max_workers = 1 if args.serial else args.max_parallel_stages
    max_gpu_stages = 1 if args.serial else args.max_parallel_gpu_stages
    runner = ProductionRunner(
        stages,
        manifest_path=manifest_path,
        repo_root=Path("."),
        executor=OptimizedGpuStageExecutor(
            repo_root=Path("."),
            output_dir=args.output_dir,
            hold_shared_workers=args.end_to_end,
        ),
        max_workers=max_workers,
        max_gpu_stages=max_gpu_stages,
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

    metrics_path = args.metrics or args.output_dir / "production_metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "total_elapsed_seconds": summary.total_elapsed_seconds,
                "max_parallel_stages": max_workers,
                "max_parallel_gpu_stages": max_gpu_stages,
                "stages": [
                    {
                        "stage_name": metric.stage_name,
                        "outcome": metric.outcome,
                        "resource": metric.resource,
                        "elapsed_seconds": metric.elapsed_seconds,
                    }
                    for metric in summary.metrics
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Production manifest: {manifest_path.resolve()}")
    print(f"Production metrics: {metrics_path.resolve()}")
    print(
        f"executed={len(summary.executed)} adopted={len(summary.adopted)} "
        f"skipped={len(summary.skipped)} total={summary.total_elapsed_seconds:.1f}s"
    )
    if args.through is None or args.through == "phase8-videos":
        print("Production phases 2-8 are complete. The local renderer can now run Phase 9.")


if __name__ == "__main__":
    main()
