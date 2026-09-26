"""Standalone B1.1 → B1.2 → B2 runner with a selectable local script library."""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Literal

from PIL import Image

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.bots.contracts import B11Output, B12Output
from ai_video_factory.bots.workflow import materialize_blocks, validate_beats
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider

DEFAULT_SCRIPTS_DIR = Path("data/input")
DEFAULT_AVATARS_DIR = Path("data/avatar")
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
        "--avatars-dir",
        type=Path,
        default=DEFAULT_AVATARS_DIR,
        help="Directory containing selectable PNG avatars (default: data/avatar).",
    )
    parser.add_argument(
        "--avatar",
        metavar="NAME",
        help="Select a PNG avatar from --avatars-dir, with or without its .png suffix.",
    )
    parser.add_argument(
        "--list-avatars",
        action="store_true",
        help="List available PNG avatars without starting inference or requiring an API key.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output root; each script gets a child folder (default: data/output).",
    )
    parser.add_argument("--max-parallel-calls", type=int, default=8)
    parser.add_argument(
        "--from",
        dest="from_stage",
        choices=("B1.1", "B1.2", "B2"),
        default="B1.1",
        help="Start at this stage using validated earlier outputs in data/output/<script>.",
    )
    args = parser.parse_args()
    if args.script_file is not None and args.script is not None:
        parser.error("Specify either a direct script path or --script, not both.")
    if args.list_scripts and args.list_avatars:
        parser.error("Choose either --list-scripts or --list-avatars.")
    if (args.list_scripts or args.list_avatars) and (
        args.script_file is not None
        or args.script is not None
        or args.avatar is not None
        or args.from_stage != "B1.1"
    ):
        parser.error("Listing cannot be combined with script or avatar selection.")
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


def available_avatars(avatars_dir: Path) -> list[Path]:
    """Discover ordinary PNG files only; never follow symlinks or nested directories."""
    if not avatars_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in avatars_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".png"
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def select_avatar(
    *,
    avatars_dir: Path,
    avatar_name: str | None = None,
    interactive: bool | None = None,
) -> Path:
    """Require an explicit avatar: by exact filename or an interactive menu."""
    avatars = available_avatars(avatars_dir)
    if not avatars:
        raise SystemExit(
            f"No PNG avatars found in {avatars_dir}. "
            "Add one or more valid .png images there before running the pipeline."
        )
    if avatar_name is not None:
        requested = (
            avatar_name if avatar_name.lower().endswith(".png") else f"{avatar_name}.png"
        )
        match = next((path for path in avatars if path.name == requested), None)
        if match is None:
            available = ", ".join(path.name for path in avatars)
            raise SystemExit(
                f"Avatar {requested!r} not found in {avatars_dir}. Available: {available}"
            )
        return match

    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit(
            "Avatar selection requires an interactive terminal. "
            "Use --avatar NAME.png or --list-avatars."
        )

    print(f"Available avatars in {avatars_dir}:")
    for number, path in enumerate(avatars, start=1):
        print(f"  {number}. {path.name}")

    while True:
        try:
            selection = input("Select an avatar number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SystemExit("Avatar selection cancelled.") from exc
        if selection.lower() in {"q", "quit"}:
            raise SystemExit("Avatar selection cancelled.")
        if selection.isdecimal() and 1 <= int(selection) <= len(avatars):
            return avatars[int(selection) - 1]
        print(f"Enter a number between 1 and {len(avatars)}, or q to cancel.")


def load_avatar_png(avatar_file: Path) -> tuple[bytes, int, int]:
    """Validate the actual PNG, not just its extension, and keep exact source bytes."""
    try:
        data = avatar_file.read_bytes()
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("The selected image is not a PNG")
            width, height = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid PNG avatar: {avatar_file}") from exc
    return data, width, height


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_output(
    output_root: Path, script_file: Path, *, avatar_file: Path | None = None
) -> Path:
    """Replace only an identifiable B-pipeline output folder for this script."""
    root = output_root.resolve()
    source = script_file.resolve()
    avatar_source = avatar_file.resolve() if avatar_file is not None else None
    cwd = Path.cwd().resolve()
    if (
        root == cwd
        or root in cwd.parents
        or source.is_relative_to(root)
        or (
            avatar_source is not None
            and (
                avatar_source.is_relative_to(root)
                or root.is_relative_to(avatar_source.parent)
            )
        )
    ):
        raise SystemExit(
            "--output must be a dedicated output directory, not the repository, "
            "a parent of it, or an input directory (script or avatar)."
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


def _prepare_resume_output(
    output_root: Path, script_file: Path, *, avatar_file: Path
) -> Path:
    root = output_root.resolve()
    source = script_file.resolve()
    avatar_source = avatar_file.resolve()
    cwd = Path.cwd().resolve()
    if (
        root == cwd
        or root in cwd.parents
        or source.is_relative_to(root)
        or avatar_source.is_relative_to(root)
        or root.is_relative_to(avatar_source.parent)
    ):
        raise SystemExit("--output must be a dedicated output directory, not an input or repository path.")
    destination = output_root / script_file.stem
    if destination.is_symlink() or destination.is_junction():
        raise SystemExit(f"Refusing to resume from a linked output directory: {destination}")
    if not destination.is_dir():
        raise SystemExit(f"No prior run directory exists for --from {script_file.stem}: {destination}")
    report_path = destination / "run_report.json"
    if not report_path.is_file() or report_path.is_symlink():
        raise SystemExit(f"Cannot resume: missing valid run report at {report_path}")
    return destination


def _load_resume_checkpoint(
    output: Path,
    *,
    start_from: str,
    script: str,
    script_sha256: str,
) -> tuple[dict[str, object], B11Output, tuple[B12Output, ...] | None]:
    try:
        report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("run"), dict):
            raise ValueError("the saved run report is malformed")
        run = report["run"]
        if run.get("script_sha256") != script_sha256:
            raise ValueError("the selected script differs from the saved run")
        b11_dir = output / "B1.1"
        b11_path = b11_dir / "output.json"
        if b11_dir.is_symlink() or b11_path.is_symlink() or not b11_path.is_file():
            raise ValueError("the saved B1.1 output is missing or linked")
        b11_data = json.loads(b11_path.read_text(encoding="utf-8"))
        b11 = B11Output.model_validate(b11_data)
        if b11.error is not None or b11.blocks is None or b11.narrative_core is None:
            raise ValueError("the saved B1.1 response is not a successful checkpoint")
        materialized = materialize_blocks(script, b11.blocks)

        b12_results: tuple[B12Output, ...] | None = None
        if start_from == "B2":
            b12_dir = output / "B1.2"
            b12_path = b12_dir / "merged_output.json"
            if b12_dir.is_symlink() or b12_path.is_symlink() or not b12_path.is_file():
                raise ValueError("the saved B1.2 merge is missing or linked")
            merged = json.loads(b12_path.read_text(encoding="utf-8"))
            if not isinstance(merged, dict):
                raise ValueError("the saved B1.2 merge is malformed")
            if (
                merged.get("pipeline_stage") != "B1.2"
                or merged.get("narrative_core") != b11.narrative_core.model_dump()
                or not isinstance(merged.get("blocks"), list)
                or len(merged["blocks"]) != len(materialized)
            ):
                raise ValueError("the saved B1.2 merge does not match the B1.1 checkpoint")
            parsed: list[B12Output] = []
            for expected_id, item, block in zip(
                range(1, len(materialized) + 1), merged["blocks"], materialized, strict=True
            ):
                response = B12Output.model_validate(
                    {
                        "pipeline_stage": "B1.2",
                        "block_id": item["block_id"],
                        "beats": item["beats"],
                        "error": None,
                    }
                )
                if response.block_id != expected_id:
                    raise ValueError("the saved B1.2 block IDs are not consecutive")
                validate_beats(response, block)
                parsed.append(response)
            b12_results = tuple(parsed)

        costs = report["api_costs"]
        timings = report["timings"]
        if (
            not isinstance(costs, dict)
            or not isinstance(costs.get("requests"), list)
            or not isinstance(timings, dict)
        ):
            raise ValueError("the saved run report lacks request or timing history")
        return report, b11, b12_results
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Cannot resume from {start_from}: {exc}") from exc


def _clear_outputs_from(output: Path, start_from: str) -> None:
    first_stage = _STAGES.index(start_from)
    for stage in _STAGES[first_stage:]:
        target = output / stage
        if target.is_symlink() or target.is_junction():
            raise SystemExit(f"Refusing to clear linked stage output: {target}")
        if target.exists():
            if not target.is_dir():
                raise SystemExit(f"Stage output is not a directory: {target}")
            shutil.rmtree(target)
    visual_plan = output / "visual_plan.json"
    if visual_plan.is_symlink():
        raise SystemExit(f"Refusing to remove linked visual plan: {visual_plan}")
    if visual_plan.exists():
        if not visual_plan.is_file():
            raise SystemExit(f"Visual plan path is not a file: {visual_plan}")
        visual_plan.unlink()


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

    def rejected_b11(
        self, attempt: int, error: str, payload: dict[str, object]
    ) -> None:
        _write(
            self._folder("B1.1", None) / f"rejected_attempt_{attempt}.json",
            {"attempt": attempt, "validation_error": error, "response": payload},
        )


class TimingLedger:
    """Monotonic wall-clock timing; parallel calls are not added into stage wall time."""

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._calls: list[dict[str, object]] = []

    def record_stage(self, stage: str, elapsed_seconds: float) -> None:
        if stage not in _STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        self._stages[stage] = self._stages.get(stage, 0.0) + elapsed_seconds

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

    def call_count(self, stage: str) -> int:
        return sum(item["stage"] == stage for item in self._calls)

    def restore(self, report: dict[str, object]) -> float:
        """Restore stage timing from a prior report; return its total elapsed time."""
        stages = report.get("stages")
        calls = report.get("calls")
        if not isinstance(stages, dict) or not isinstance(calls, list):
            raise ValueError("Cannot restore malformed timing history")
        for stage in _STAGES:
            entry = stages.get(stage)
            if isinstance(entry, dict) and isinstance(entry.get("elapsed_seconds"), (int, float)):
                self._stages[stage] = float(entry["elapsed_seconds"])
        for call in calls:
            if (
                not isinstance(call, dict)
                or call.get("stage") not in _STAGES
                or not isinstance(call.get("elapsed_seconds"), (int, float))
            ):
                raise ValueError("Cannot restore malformed timing call history")
            self._calls.append(dict(call))
        elapsed = report.get("run_elapsed_seconds")
        if not isinstance(elapsed, (int, float)):
            raise ValueError("Cannot restore missing total run time")
        return float(elapsed)

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
    # Git does not track empty runtime directories: restore the local libraries.
    if args.script_file is None:
        args.scripts_dir.mkdir(parents=True, exist_ok=True)
    args.avatars_dir.mkdir(parents=True, exist_ok=True)

    if args.list_scripts:
        scripts = available_scripts(args.scripts_dir)
        if not scripts:
            print(f"No .txt scripts found in {args.scripts_dir}.")
        else:
            print(f"Available scripts in {args.scripts_dir}:")
            for path in scripts:
                print(f"  {path.name}")
        return

    if args.list_avatars:
        avatars = available_avatars(args.avatars_dir)
        if not avatars:
            print(f"No PNG avatars found in {args.avatars_dir}.")
        else:
            print(f"Available avatars in {args.avatars_dir}:")
            for path in avatars:
                print(f"  {path.name}")
        return

    script_file = select_script(
        scripts_dir=args.scripts_dir,
        script_name=args.script,
        script_file=args.script_file,
    )
    avatar_file = select_avatar(avatars_dir=args.avatars_dir, avatar_name=args.avatar)
    avatar_bytes, avatar_width, avatar_height = load_avatar_png(avatar_file)
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
    script_sha256 = hashlib.sha256(source_bytes).hexdigest()
    ledger = ApiCostLedger()
    timings = TimingLedger()
    resume_elapsed_offset = 0.0
    resume_expected_calls: dict[str, int | None] | None = None
    previous_b11: B11Output | None = None
    previous_b12: tuple[B12Output, ...] | None = None
    if args.from_stage == "B1.1":
        output = _prepare_output(output_root, script_file, avatar_file=avatar_file)
    else:
        output = _prepare_resume_output(output_root, script_file, avatar_file=avatar_file)
        previous_report, previous_b11, previous_b12 = _load_resume_checkpoint(
            output,
            start_from=args.from_stage,
            script=script,
            script_sha256=script_sha256,
        )
        try:
            previous_costs = previous_report["api_costs"]
            previous_timing = previous_report["timings"]
            ledger.restore_requests(previous_costs["requests"])
            resume_elapsed_offset = timings.restore(previous_timing)
            prior_counts = {stage: timings.call_count(stage) for stage in _STAGES}
            rerun_from_index = _STAGES.index(args.from_stage)
            block_count = len(previous_b11.blocks or [])
            resume_expected_calls = {
                stage: prior_counts[stage]
                + (block_count if stage_index >= rerun_from_index else 0)
                for stage_index, stage in enumerate(_STAGES)
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Cannot restore prior run accounting: {exc}") from exc
        _clear_outputs_from(output, args.from_stage)

    # Snapshot the selected PNG: later library changes cannot silently alter this run.
    (output / "avatar.png").write_bytes(avatar_bytes)
    avatar_metadata = {
        "filename": avatar_file.name,
        "source": avatar_file.as_posix(),
        "file": "avatar.png",
        "sha256": hashlib.sha256(avatar_bytes).hexdigest(),
        "width": avatar_width,
        "height": avatar_height,
    }
    run_metadata = {
        "script_file": script_file.name,
        "avatar": avatar_metadata,
        "script_sha256": script_sha256,
        "model": settings.openai_b_model,
        "reasoning_effort": settings.openai_b_reasoning_effort,
    }
    if args.from_stage != "B1.1":
        run_metadata["resumed_from"] = args.from_stage
    artifacts = BotArtifacts(output)

    def save_report(
        status: Literal["running", "completed", "failed"],
        blocks: int | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        costs = ledger.report(
            blocks=blocks,
            run_status=status,
            expected_b11_calls=max(1, timings.call_count("B1.1")),
            expected_calls=resume_expected_calls,
        )
        timing_report = timings.report(
            run_seconds=resume_elapsed_offset + perf_counter() - run_started,
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
            start_from=args.from_stage,
            previous_b11=previous_b11,
            previous_b12=previous_b12,
            on_input=artifacts.input,
            on_output=artifacts.output,
            on_rejected_output=artifacts.rejected_b11,
            on_api_response=ledger.record,
            on_call_duration=timings.record_call,
            on_stage_duration=timings.record_stage,
            on_stage_complete=stage_complete,
        )
        visual_plan = result.visual_plan()
        # This application-owned binding is intentionally absent from audited B2 payloads.
        visual_plan["avatar"] = avatar_metadata
        _write(output / "visual_plan.json", visual_plan)
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
