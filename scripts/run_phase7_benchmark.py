import argparse
import json
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain.gpu import LTXBenchmarkProfile
from ai_video_factory.workflows.gpu_benchmark import benchmark_ltx_command

DEFAULT_LTX_SOURCE = "Lightricks/LTX-2@a95ab856bf29407b6b066ede0abe1846050db56c"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark one reproducible LTX-2.5 command on the current NVIDIA GPU."
    )
    parser.add_argument("--label", required=True, help="Stable case label used in the report.")
    parser.add_argument("--ltx-source", default=DEFAULT_LTX_SOURCE)
    parser.add_argument("--pipeline", default="distilled")
    parser.add_argument(
        "--quantization",
        choices=("bf16", "fp8-cast", "fp8-scaled-mm"),
        default="bf16",
    )
    parser.add_argument("--offload", choices=("none", "cpu", "disk"), default="none")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--num-frames", type=int, required=True)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--measured-runs", type=int, default=3)
    parser.add_argument("--sample-interval", type=float, default=0.2)
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=settings.temp_dir / "phase7" / "benchmark-output.mp4",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=settings.output_dir / "phase7" / "ltx_benchmark.json",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command after --; include exactly one literal {output} placeholder.",
    )
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("an LTX command is required after --")
    return args


def main() -> None:
    args = parse_args()
    profile = LTXBenchmarkProfile(
        label=args.label,
        ltx_source=args.ltx_source,
        pipeline=args.pipeline,
        quantization=args.quantization,
        offload=args.offload,
        width=args.width,
        height=args.height,
        num_frames=args.num_frames,
        fps=args.fps,
        warmup_runs=args.warmup_runs,
        measured_runs=args.measured_runs,
    )
    report = benchmark_ltx_command(
        profile,
        args.command,
        output_path=args.sample_output,
        sample_interval_seconds=args.sample_interval,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )

    print(f"Phase 7 LTX benchmark complete. Report: {args.report.resolve()}")
    print(
        f"Mean generation time: {report.mean_duration_seconds:.2f}s; "
        f"peak GPU memory: {report.max_peak_gpu_memory_mib} MiB"
    )


if __name__ == "__main__":
    main()
