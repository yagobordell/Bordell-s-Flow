from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_video_factory.config import settings
from ai_video_factory.domain.gpu import LTXBenchmarkProfile, LTXBenchmarkReport
from ai_video_factory.workflows.gpu_benchmark import benchmark_ltx_command

DEFAULT_LTX_SOURCE = "Lightricks/LTX-2@a95ab856bf29407b6b066ede0abe1846050db56c"
LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
QUANTIZATIONS = {"bf16", "fp8-cast", "fp8-scaled-mm"}
OFFLOADS = {"none", "cpu", "disk"}


@dataclass(frozen=True)
class BenchmarkCase:
    label: str
    quantization: str
    offload: str


DEFAULT_CASES = (
    BenchmarkCase("distilled-bf16-none", "bf16", "none"),
    BenchmarkCase("distilled-fp8-none", "fp8-cast", "none"),
    BenchmarkCase("distilled-fp8-cpu", "fp8-cast", "cpu"),
)


def _parse_case(value: str) -> BenchmarkCase:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("case must use LABEL:QUANTIZATION:OFFLOAD")
    label, quantization, offload = parts
    if not LABEL_PATTERN.fullmatch(label):
        raise argparse.ArgumentTypeError(f"invalid case label: {label}")
    if quantization not in QUANTIZATIONS:
        raise argparse.ArgumentTypeError(f"invalid quantization: {quantization}")
    if offload not in OFFLOADS:
        raise argparse.ArgumentTypeError(f"invalid offload mode: {offload}")
    return BenchmarkCase(label, quantization, offload)


def _validate_command_template(command: list[str]) -> None:
    joined = "\0".join(command)
    if joined.count("{output}") != 1:
        raise ValueError("command must contain exactly one literal {output} placeholder")
    for placeholder in (
        "{prompt}",
        "{conditioning_image}",
        "{seed}",
        "{quantization_args}",
        "{offload_args}",
    ):
        if command.count(placeholder) != 1:
            raise ValueError(f"command must contain one standalone {placeholder} placeholder")


def _render_case_command(
    command: list[str],
    case: BenchmarkCase,
    *,
    prompt: str,
    conditioning_image: Path,
    seed: int,
) -> list[str]:
    _validate_command_template(command)
    rendered: list[str] = []
    for token in command:
        if token == "{quantization_args}":
            if case.quantization != "bf16":
                rendered.extend(("--quantization", case.quantization))
        elif token == "{offload_args}":
            if case.offload != "none":
                rendered.extend(("--offload", case.offload))
        elif token == "{prompt}":
            rendered.append(prompt)
        elif token == "{conditioning_image}":
            rendered.append(str(conditioning_image.resolve()))
        elif token == "{seed}":
            rendered.append(str(seed))
        else:
            rendered.append(token)
    return rendered


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _matrix_document(
    *,
    hardware_label: str,
    command_template: list[str],
    prompt: str,
    conditioning_image: Path,
    seed: int,
    sample_interval_seconds: float,
    reports: list[tuple[Path, LTXBenchmarkReport]],
) -> dict:
    rows = [
        {
            "label": report.profile.label,
            "quantization": report.profile.quantization,
            "offload": report.profile.offload,
            "devices": [device.model_dump(mode="json") for device in report.devices],
            "mean_duration_seconds": report.mean_duration_seconds,
            "median_duration_seconds": report.median_duration_seconds,
            "mean_end_to_end_frames_per_second": report.mean_end_to_end_frames_per_second,
            "max_peak_gpu_memory_mib": report.max_peak_gpu_memory_mib,
            "report": path.name,
        }
        for path, report in reports
    ]
    fastest = min(rows, key=lambda row: row["mean_duration_seconds"])
    first_profile = reports[0][1].profile
    return {
        "schema_version": "1",
        "created_at": datetime.now(UTC).isoformat(),
        "hardware_label": hardware_label,
        "workload": {
            "ltx_source": first_profile.ltx_source,
            "pipeline": first_profile.pipeline,
            "width": first_profile.width,
            "height": first_profile.height,
            "num_frames": first_profile.num_frames,
            "fps": first_profile.fps,
            "warmup_runs": first_profile.warmup_runs,
            "measured_runs": first_profile.measured_runs,
            "sample_interval_seconds": sample_interval_seconds,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "conditioning_image_sha256": _sha256_file(conditioning_image),
            "seed": seed,
        },
        "command_template": command_template,
        "cases": rows,
        "fastest_case_by_mean_duration": fastest["label"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and persist the complete reproducible Phase 7 LTX benchmark matrix."
    )
    parser.add_argument("--hardware-label", required=True)
    parser.add_argument("--ltx-source", default=DEFAULT_LTX_SOURCE)
    parser.add_argument("--pipeline", default="distilled")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--conditioning-image", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--num-frames", type=int, required=True)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--measured-runs", type=int, default=3)
    parser.add_argument("--sample-interval", type=float, default=0.2)
    parser.add_argument(
        "--case",
        action="append",
        type=_parse_case,
        help="Repeatable LABEL:QUANTIZATION:OFFLOAD; defaults to the canonical three cases.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.output_dir / "phase7" / "benchmarks",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=settings.temp_dir / "phase7" / "benchmarks",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help=(
            "Command after --; include the documented workload and matrix placeholders."
        ),
    )
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("an LTX command is required after --")
    if not LABEL_PATTERN.fullmatch(args.hardware_label):
        parser.error("--hardware-label must contain lowercase letters, numbers, '.', '_' or '-'")
    if not args.conditioning_image.is_file():
        parser.error("--conditioning-image must point to an existing file")
    if args.width <= 0 or args.height <= 0 or args.num_frames <= 0 or args.fps <= 0:
        parser.error("workload dimensions, frames, and fps must be positive")
    if args.warmup_runs < 0 or args.measured_runs < 1 or args.sample_interval <= 0:
        parser.error("run counts and sample interval are invalid")
    try:
        _validate_command_template(args.command)
    except ValueError as exc:
        parser.error(str(exc))
    args.case = args.case or list(DEFAULT_CASES)
    labels = [case.label for case in args.case]
    if len(labels) != len(set(labels)):
        parser.error("case labels must be unique")
    return args


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir / args.hardware_label
    temp_dir = args.temp_dir / args.hardware_label
    reports: list[tuple[Path, LTXBenchmarkReport]] = []

    for case in args.case:
        label = f"{args.hardware_label}-{case.label}"
        profile = LTXBenchmarkProfile(
            label=label,
            ltx_source=args.ltx_source,
            pipeline=args.pipeline,
            quantization=case.quantization,
            offload=case.offload,
            width=args.width,
            height=args.height,
            num_frames=args.num_frames,
            fps=args.fps,
            warmup_runs=args.warmup_runs,
            measured_runs=args.measured_runs,
        )
        report = benchmark_ltx_command(
            profile,
            _render_case_command(
                args.command,
                case,
                prompt=args.prompt,
                conditioning_image=args.conditioning_image,
                seed=args.seed,
            ),
            output_path=temp_dir / f"{case.label}.mp4",
            sample_interval_seconds=args.sample_interval,
        )
        report_path = output_dir / f"{case.label}.json"
        _write_json(report_path, report.model_dump(mode="json"))
        reports.append((report_path, report))
        print(
            f"case={case.label} mean={report.mean_duration_seconds:.2f}s "
            f"peak_mib={report.max_peak_gpu_memory_mib} report={report_path}"
        )

    matrix_path = output_dir / "matrix.json"
    _write_json(
        matrix_path,
        _matrix_document(
            hardware_label=args.hardware_label,
            command_template=args.command,
            prompt=args.prompt,
            conditioning_image=args.conditioning_image,
            seed=args.seed,
            sample_interval_seconds=args.sample_interval,
            reports=reports,
        ),
    )
    print(f"Phase 7 benchmark matrix complete: {matrix_path.resolve()}")


if __name__ == "__main__":
    main()
