"""Standalone B1.1 → B1.2 → B2 runner with a selectable local script library."""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path
from time import perf_counter
from typing import Literal

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider

DEFAULT_SCRIPTS_DIR = Path("data/input")
_STAGES = ("B1.1", "B1.2", "B2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "script_file",
        nargs="?",
        type=Path,
        help="Optional direct path to a UTF-8 script (for existing commands/automation).",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=DEFAULT_SCRIPTS_DIR,
        help="Directory containing selectable .txt scripts (default: data/input).",
    )
    parser.add_argument(
        "--script",
        metavar="NAME",
        help="Select a .txt file by name from --scripts-dir, with or without its .txt suffix.",
    )
    parser.add_argument(
        "--list-scripts",
        action="store_true",
        help="List available scripts without starting inference or requiring an API key.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root; each script gets a child folder (default: data/output).",
    )
    parser.add_argument("--max-parallel-calls", type=int, default=8)
    args = parser.parse_args()
    if args.script_file is not None and args.script is not None:
        parser.error("Specify either a direct script path or --script, not both.")
    if args.list_scripts and (args.script_file is not None or args.script is not None):
        parser.error("--list-scripts cannot be combined with a script selection.")
    return args


def available_scripts(scripts_dir: Path) -> list[Path]:
    """Discover only ordinary .txt files from the selected local library."""
    if not scripts_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in scripts_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".txt"
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def select_script(
    *,
    scripts_dir: Path,
    script_name: str | None = None,
    script_file: Path | None = None,
    interactive: bool | None = None,
) -> Path:
    """Select one file without changing its content or moving it."""
    if script_name is not None and script_file is not None:
        raise SystemExit("Specify either a script path or --script, not both.")
    if script_file is not None:
        if not script_file.is_file():
            raise SystemExit(f"Script file not found: {script_file}")
        return script_file

    scripts = available_scripts(scripts_dir)
    if not scripts:
        raise SystemExit(
            f"No .txt scripts found in {scripts_dir}. "
            "Please add one or more UTF-8 .txt scripts there."
        )
    if script_name is not None:
        requested = (
            script_name if script_name.lower().endswith(".txt") else f"{script_name}.txt"
        )
        match = next((path for path in scripts if path.name == requested), None)
        if match is None:
            available = ", ".join(path.name for path in scripts)
            raise SystemExit(
                f"Script {requested!r} not found in {scripts_dir}. Available: {available}"
            )
        return match

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit(
            "Script selection requires an interactive terminal. "
            "Use --script NAME.txt or --list-scripts."
        )

    print(f"Available scripts in {scripts_dir}:")
    for number, path in enumerate(scripts, start=1):
        print(f"  {number}. {path.name}")

    while True:
        try:
            selection = input("Select a script number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Script selection cancelled.") from exc
        if selection.lower() in {"q", "quit"}:
            raise SystemExit("Script selection cancelled.")
        if selection.isdecimal() and 1 <= int(selection) <= len(scripts):
            return scripts[int(selection) - 1]
        print(f"Enter a number between 1 and {len(scripts)}, or q to cancel.")


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_output(output_root: Path, script_file: Path) -> Path:
    """Replace only an identifiable B-pipeline output folder for this script."""
    root = output_root.resolve()
    source = script_file.resolve()
    cwd = Path.cwd().resolve()
    if root == cwd or root in cwd.parents or source.is_relative_to(root):
        raise SystemExit(
            "--output must be a dedicated output directory, not the repository, "
            "a parent of it, or the selected script's input directory."
        )
    destination = output_root / script_file.stem
    if destination.is_symlink() or destination.is_junction():
        raise SystemExit(f"Refusing to delete a linked output directory: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise SystemExit(f"Output path is not a directory: {destination}")
        recognized = (
            destination / "run_report.json",
            destination / ".b_pipeline_run.json",
            destination / "B1.1" / "input.json",
            destination / "b1_1.json",  # Previous standalone runner layout.
            destination / "visual_plan.json",
        )
        if any(destination.iterdir()) and not any(path.is_file() for path in recognized):
            raise SystemExit(
                f"Refusing to delete unrelated files in {destination}; "
                "choose a dedicated --output root."
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    return destination


class BotArtifacts:
    """Persist the exact request payload and validated response of every bot call."""

    def __init__(self, output: Path) -> None:
        self._output = output

    def _folder(self, stage: str, block_id: int | None) -> Path:
        if stage not in _STAGES:
            raise ValueError(f"Unknown B pipeline stage: {stage}")
        if stage == "B1.1":
            if block_id is not None:
                raise ValueError("B1.1 must not have a block ID")
            return self._output / stage
        if block_id is None or block_id < 1:
            raise ValueError(f"{stage} requires a positive block ID")
        return self._output / stage / f"block_{block_id}"

    def input(self, stage: str, block_id: int | None, payload: dict[str, object]) -> None:
        _write(self._folder(stage, block_id) / "input.json", payload)

    def output(self, stage: str, block_id: int | None, payload: dict[str, object]) -> None:
        _write(self._folder(stage, block_id) / "output.json", payload)


class TimingLedger:
    """Monotonic wall-clock timing; parallel calls are not added into stage wall time."""

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._calls: list[dict[str, object]] = []

    def record_stage(self, stage: str, elapsed_seconds: float) -> None:
        if stage not in _STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._stages[stage] = elapsed_seconds

    def record_call(self, stage: str, block_id: int | None, elapsed_seconds: float) -> None:
        if stage not in _STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._calls.append(
            {
                "stage": stage,
                "block_id": block_id,
                "elapsed_seconds": round(elapsed_seconds, 3),
            }
        )

    def report(self, *, run_seconds: float, status: str) -> dict[str, object]:
        calls = sorted(
            self._calls,
            key=lambda item: (_STAGES.index(str(item["stage"])), item["block_id"] or 0),
        )
        return {
            "unit": "seconds",
            "status": status,
            "run_elapsed_seconds": round(run_seconds, 3),
            "stages": {
                stage: {
                    "elapsed_seconds": (
                        round(self._stages[stage], 3) if stage in self._stages else None
                    ),
                    "call_count": len([item for item in calls if item["stage"] == stage]),
                    "sum_call_seconds": round(
                        sum(
                            float(item["elapsed_seconds"])
                            for item in calls
                            if item["stage"] == stage
                        ),
                        3,
                    ),
                }
                for stage in _STAGES
            },
            "calls": calls,
        }


def _print_metric(name: str, seconds: float, cost: object) -> None:
    """Print a single completed bot/run line, flushing as soon as it finishes."""
    price = f"${cost}" if cost is not None else "coste no disponible"
    print(f"  {name} total: {seconds:.3f} s {price}", flush=True)


async def main() -> None:
    args = parse_args()
    # Git does not track empty directories: restore the local input root on fresh clones.
    if args.script_file is None:
        args.scripts_dir.mkdir(parents=True, exist_ok=True)

    if args.list_scripts:
        scripts = available_scripts(args.scripts_dir)
        if not scripts:
            print(f"No .txt scripts found in {args.scripts_dir}.")
        else:
            print(f"Available scripts in {args.scripts_dir}:")
            for path in scripts:
                print(f"  {path.name}")
        return

    script_file = select_script(
        scripts_dir=args.scripts_dir,
        script_name=args.script,
        script_file=args.script_file,
    )
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing")

    if args.max_parallel_calls < 1:
        raise SystemExit("--max-parallel-calls must be positive")
    # Preserve every source character: no strip(), newline normalization or rewriting.
    try:
        source_bytes = script_file.read_bytes()
        script = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Script must be UTF-8: {script_file}") from exc
    if not script.strip():
        raise SystemExit(f"Script is empty: {script_file}")

    run_started = perf_counter()
    output_root = args.output if args.output is not None else settings.output_dir
    output = _prepare_output(output_root, script_file)
    run_metadata = {
        "script_file": script_file.name,
        "script_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "model": settings.openai_b_model,
        "reasoning_effort": settings.openai_b_reasoning_effort,
    }
    artifacts = BotArtifacts(output)
    ledger = ApiCostLedger()
    timings = TimingLedger()

    def save_report(
        status: Literal["running", "completed", "failed"],
        blocks: int | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        costs = ledger.report(blocks=blocks, run_status=status)
        timing_report = timings.report(
            run_seconds=perf_counter() - run_started,
            status=status,
        )
        _write(
            output / "run_report.json",
            {
                "run": {**run_metadata, "status": status},
                "api_costs": costs,
                "timings": timing_report,
            },
        )
        return costs, timing_report

    def stage_complete(
        stage: str,
        blocks: int,
        merged_output: dict[str, object] | None,
    ) -> None:
        if merged_output is not None:
            _write(output / stage / "merged_output.json", merged_output)
        costs, timing_report = save_report("running", blocks)
        elapsed = timing_report["stages"][stage]["elapsed_seconds"]
        if elapsed is None:
            raise RuntimeError(f"Missing elapsed time for completed stage {stage}")
        _print_metric(stage, elapsed, costs["stages"][stage]["estimated_cost_usd"])

    save_report("running", None)
    try:
        provider = OpenAIProvider(
            api_key=settings.openai_api_key,
            reasoning_effort=settings.openai_b_reasoning_effort,
            service_tier="default",
        )
        result = await run_b_pipeline(
            script,
            provider=provider,
            model=settings.openai_b_model,
            max_parallel_calls=args.max_parallel_calls,
            on_input=artifacts.input,
            on_output=artifacts.output,
            on_api_response=ledger.record,
            on_call_duration=timings.record_call,
            on_stage_duration=timings.record_stage,
            on_stage_complete=stage_complete,
        )
        _write(output / "visual_plan.json", result.visual_plan())
    except Exception:
        _, timing_report = save_report("failed", None)
        _print_metric("Run", timing_report["run_elapsed_seconds"], None)
        raise

    costs, timing_report = save_report("completed", len(result.b12))
    _print_metric(
        "Run",
        timing_report["run_elapsed_seconds"],
        costs["estimated_total_usd"],
    )


if __name__ == "__main__":
    asyncio.run(main())
