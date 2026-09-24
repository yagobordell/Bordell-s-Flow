from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.inference.capacity_controller import read_capacity_controller_health
from ai_video_factory.workflows.production_runner import (
    PRODUCTION_STAGE_NAMES,
    ProductionRunner,
    ProductionStage,
    ProductionStageBlocked,
    SubprocessStageExecutor,
    build_production_stages,
)


def _autoscaler_enabled() -> bool:
    return os.getenv("SALAD_AUTOSCALER_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _capacity_health_check():
    if not _autoscaler_enabled():
        return None

    postgres_dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not postgres_dsn:
        raise RuntimeError(
            "POSTGRES_DSN is required while the global Salad capacity controller is enabled"
        )
    max_heartbeat_age = _positive_float_env(
        "SALAD_CAPACITY_CONTROLLER_MAX_HEARTBEAT_AGE_SECONDS",
        120.0,
    )
    max_reconcile_age = _positive_float_env(
        "SALAD_CAPACITY_CONTROLLER_MAX_RECONCILE_AGE_SECONDS",
        120.0,
    )

    def check() -> None:
        health = read_capacity_controller_health(
            postgres_dsn,
            max_heartbeat_age_seconds=max_heartbeat_age,
            max_reconcile_age_seconds=max_reconcile_age,
        )
        if not health.healthy:
            raise RuntimeError(
                "Salad capacity controller is unhealthy: "
                f"controller_id={health.controller_id or '<none>'} "
                f"status={health.status or '<none>'} reason={health.reason}"
            )

    return check


class OptimizedGpuStageExecutor:
    """Run provider-neutral stages when the global capacity controller owns Salad."""

    def __init__(
        self,
        *,
        repo_root: Path,
        output_dir: Path,
        hold_shared_workers: bool = False,
        narration_language: str = "en",
    ) -> None:
        self._repo_root = repo_root
        self._output_dir = output_dir
        self._hold_shared_workers = hold_shared_workers
        self._narration_language = narration_language
        self._default = SubprocessStageExecutor(repo_root=repo_root)

    def __call__(self, stage: ProductionStage) -> None:
        if _autoscaler_enabled():
            self._default(stage)
            return

        arguments = self._controlled_arguments(stage.name)
        if arguments is None:
            self._default(stage)
            return

        executable = "powershell.exe" if os.name == "nt" else "pwsh"
        command = [executable, "-NoProfile"]
        if os.name == "nt":
            command.extend(["-ExecutionPolicy", "Bypass"])
        command.extend(["-File", *arguments])
        self._default.run_command(command)

    def cancel_running(self) -> None:
        self._default.cancel_running()

    def _controlled_arguments(self, stage_name: str) -> list[str] | None:
        output = self._output_dir
        if stage_name == "phase4-reference-assets":
            arguments = [
                "scripts/pipeline/run_phase4_assets_controlled.ps1",
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
                "scripts/pipeline/run_phase5_audio_controlled.ps1",
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
                "scripts/pipeline/run_phase5_alignment_controlled.ps1",
                "-Source",
                str(output / "phase2" / "source_script.json"),
                "-Narration",
                str(output / "phase5" / "narration.json"),
                "-Audio",
                str(output / "phase5" / "narration.wav"),
                "-Output",
                str(output / "phase5" / "narration_words.json"),
                "-Language",
                self._narration_language,
                "-NonInteractive",
            ]
        if stage_name == "phase6-keyframes":
            arguments = [
                "scripts/pipeline/run_phase6_keyframes_controlled.ps1",
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
                "scripts/pipeline/run_phase8_videos_controlled.ps1",
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
        if stage_name == "phase8-upscale":
            return [
                "scripts/pipeline/run_phase8_upscale_controlled.ps1",
                "-Clips",
                str(output / "phase8" / "video_clips.json"),
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
        "--narration-language",
        default="en",
        help="Language hint passed explicitly to Whisper alignment. Defaults to en.",
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
        help="Stop after the selected stage instead of running through Phase 8 upscale.",
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
        help=(
            "Write stage wall-clock metrics JSON. "
            "Defaults to <output-dir>/production_metrics.json."
        ),
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
        narration_language=args.narration_language,
    )
    max_workers = 1 if args.serial else args.max_parallel_stages
    max_gpu_stages = 1 if args.serial else args.max_parallel_gpu_stages
    capacity_health_check = _capacity_health_check()
    runner = ProductionRunner(
        stages,
        manifest_path=manifest_path,
        repo_root=Path("."),
        executor=OptimizedGpuStageExecutor(
            repo_root=Path("."),
            output_dir=args.output_dir,
            hold_shared_workers=args.end_to_end,
            narration_language=args.narration_language,
        ),
        max_workers=max_workers,
        max_gpu_stages=max_gpu_stages,
        health_check=capacity_health_check,
        health_check_interval_seconds=15.0,
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
    elapsed_by_stage = {
        metric.stage_name: metric.elapsed_seconds
        for metric in summary.metrics
    }
    critical_path_by_stage: dict[str, float] = {}
    for stage in stages:
        if stage.name not in elapsed_by_stage:
            continue
        dependency_path = max(
            (critical_path_by_stage.get(name, 0.0) for name in stage.dependencies),
            default=0.0,
        )
        critical_path_by_stage[stage.name] = (
            dependency_path + elapsed_by_stage[stage.name]
        )
    serial_stage_seconds = sum(elapsed_by_stage.values())
    critical_path_seconds = max(critical_path_by_stage.values(), default=0.0)
    metrics_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "total_elapsed_seconds": summary.total_elapsed_seconds,
                "serial_stage_seconds": serial_stage_seconds,
                "critical_path_seconds": critical_path_seconds,
                "dag_overlap_saved_seconds": max(
                    0.0,
                    serial_stage_seconds - summary.total_elapsed_seconds,
                ),
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
    if args.through is None or args.through == "phase8-upscale":
        print("Production phases 2-8 plus 1440p upscale are complete. Phase 9 can now run.")


if __name__ == "__main__":
    main()
