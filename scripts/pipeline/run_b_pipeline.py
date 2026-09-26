"""Standalone validated B1.1 → B1.2 → B2 pilot; does not alter legacy artifacts."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_video_factory.bots import run_b_pipeline
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script_file", type=Path, help="Authoritative UTF-8 narrated script")
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "b_pipeline",
        help="Isolated canonical B1/B2 output directory (no legacy overwrites)",
    )
    parser.add_argument("--max-parallel-calls", type=int, default=8)
    return parser.parse_args()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def main() -> None:
    args = parse_args()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing")
    if not args.script_file.is_file():
        raise SystemExit(f"Script file not found: {args.script_file}")
    # In contrast to the old phase2 runner, do not strip or normalize any source characters.
    script = args.script_file.read_bytes().decode("utf-8")
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
    _write(args.output / "b1_1.json", result.b11.model_dump())
    for item in result.b12:
        _write(args.output / "b1_2" / f"block_{item.block_id}.json", item.model_dump())
    for item in result.b2:
        if item.blocks is None:
            raise RuntimeError("B2 returned no validated blocks")
        _write(args.output / "b2" / f"block_{item.blocks[0].block_id}.json", item.model_dump())
    _write(args.output / "visual_plan.json", result.visual_plan())
    print(f"B1.1/B1.2/B2 validated; canonical artifacts: {args.output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
