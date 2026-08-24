import argparse
import asyncio
from pathlib import Path

from ai_video_factory.agents.director import DirectorAgent
from ai_video_factory.agents.scriptwriter import ScriptWriterAgent
from ai_video_factory.config import settings
from ai_video_factory.providers import OpenAIProvider
from ai_video_factory.workflows.create_video import create_script_and_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a script and storyboard from a topic.")
    parser.add_argument("topic", help="Video topic, for example: 'La historia de los samuráis'")
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.output_dir / "phase1",
        help="Directory where Phase 1 JSON artifacts will be written.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is missing. Copy .env.example to .env and add your key.")

    provider = OpenAIProvider(api_key=settings.openai_api_key)
    writer = ScriptWriterAgent(provider=provider, model=settings.openai_model)
    director = DirectorAgent(provider=provider, model=settings.openai_model)

    project, script, plan = await create_script_and_plan(
        args.topic,
        writer=writer,
        director=director,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "project.json").write_text(project.model_dump_json(indent=2), encoding="utf-8")
    (args.output / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
    (args.output / "video_plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    print(f"Phase 1 complete. Artifacts written to: {args.output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
