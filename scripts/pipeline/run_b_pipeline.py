"""Standalone B1.1 → B1.2 → B2 runner with a selectable local script library."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider

DEFAULT_SCRIPTS_DIR = Path("data/input/scripts")


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
        help="Output directory (default: data/output/b_pipeline/<selected-script-name>).",
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

    # Preserve every source character: no strip(), newline normalization or rewriting.
    try:
        script = script_file.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Script must be UTF-8: {script_file}") from exc
    if not script.strip():
        raise SystemExit(f"Script is empty: {script_file}")

    output = (
        args.output
        if args.output is not None
        else settings.output_dir / "b_pipeline" / script_file.stem
    )
    print(f"Selected script: {script_file}")
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
    )

    _write(output / "b1_1.json", result.b11.model_dump())
    for item in result.b12:
        _write(output / "b1_2" / f"block_{item.block_id}.json", item.model_dump())
    for item in result.b2:
        if item.blocks is None:
            raise RuntimeError("B2 returned no validated blocks")
        _write(output / "b2" / f"block_{item.blocks[0].block_id}.json", item.model_dump())
    _write(output / "visual_plan.json", result.visual_plan())
    print(f"B1.1/B1.2/B2 validated; canonical artifacts: {output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
