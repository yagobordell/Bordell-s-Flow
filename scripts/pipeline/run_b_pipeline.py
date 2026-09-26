"""Standalone B1.1 → B1.2 → B2 runner with a selectable local script library."""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider

DEFAULT_SCRIPTS_DIR = Path("data/input/scripts")
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
        help="Directory containing selectable .txt scripts (default: data/input/scripts).",
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
        help="Output root; each script gets a child folder (default: data/output/b_pipeline).",
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
            "Create that directory and add one or more UTF-8 .txt scripts."
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
    if destination.is_symlink():
        raise SystemExit(f"Refusing to delete a symlinked output directory: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise SystemExit(f"Output path is not a directory: {destination}")
        recognized = (
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
        print(f"Removed previous B-pipeline output: {destination}")
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


def _print_costs(report: dict[str, object]) -> None:
    stages = report["stages"]
    if not isinstance(stages, dict):
        raise RuntimeError("Invalid API cost report")
    print("OpenAI API costs (USD, estimated from response usage):")
    for stage in _STAGES:
        entry = stages[stage]
        cost = entry["estimated_cost_usd"]
        print(f"  {stage}: USD {cost}" if cost is not None else f"  {stage}: unavailable")
    total = report["estimated_total_usd"]
    if total is None:
        print(
            "  Total: unavailable (incomplete or unpriced API usage); "
            f"priced subtotal: USD {report['priced_subtotal_usd']}"
        )
    else:
        print(f"  Total: USD {total}")
    print("  This token-based estimate is not an OpenAI invoice.")


async def main() -> None:
    args = parse_args()

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

    output_root = args.output if args.output is not None else settings.output_dir / "b_pipeline"
    output = _prepare_output(output_root, script_file)
    _write(
        output / ".b_pipeline_run.json",
        {
            "script_file": script_file.name,
            "script_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "model": settings.openai_b_model,
            "reasoning_effort": settings.openai_b_reasoning_effort,
        },
    )
    print(f"Selected script: {script_file}")
    artifacts = BotArtifacts(output)
    ledger = ApiCostLedger()
    provider = OpenAIProvider(
        api_key=settings.openai_api_key,
        reasoning_effort=settings.openai_b_reasoning_effort,
        service_tier="default",
    )
    try:
        result = await run_b_pipeline(
            script,
            provider=provider,
            model=settings.openai_b_model,
            max_parallel_calls=args.max_parallel_calls,
            on_input=artifacts.input,
            on_output=artifacts.output,
            on_api_response=ledger.record,
        )
        _write(output / "visual_plan.json", result.visual_plan())
    except Exception:
        report = ledger.report(blocks=None, run_status="failed")
        _write(output / "api_costs.json", report)
        _print_costs(report)
        raise

    report = ledger.report(blocks=len(result.b12), run_status="completed")
    _write(output / "api_costs.json", report)
    _print_costs(report)
    print(f"B1.1/B1.2/B2 validated; canonical artifacts: {output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
