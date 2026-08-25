import asyncio
from typing import Any

from pydantic import BaseModel

from ai_video_factory.agents.director import DirectedStoryboard, DirectorAgent
from ai_video_factory.domain import ProjectConfig, Script, StoryboardScene


class FakeStructuredProvider:
    def __init__(self, result: DirectedStoryboard) -> None:
        self.result = result
        self.last_call: dict[str, Any] | None = None

    async def generate_structured(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[BaseModel],
    ) -> BaseModel:
        self.last_call = {
            "model": model,
            "instructions": instructions,
            "input_text": input_text,
            "output_type": output_type,
        }
        return self.result


def test_director_builds_video_plan_with_real_project_state() -> None:
    project = ProjectConfig(topic="La historia de los samuráis")
    script = Script(
        title="Samuráis",
        hook="No siempre fueron la élite de Japón.",
        narration="No siempre fueron la élite de Japón. Su poder creció durante siglos.",
    )
    storyboard = DirectedStoryboard(
        title="Samuráis: de guerreros a leyenda",
        visual_style="cinematic historical realism, feudal Japan, dramatic natural light",
        scenes=[
            StoryboardScene(
                id=1,
                narration="No siempre fueron la élite de Japón.",
                visual_prompt="Feudal Japanese mounted warrior at dawn, cinematic realism",
                duration_hint_seconds=4.0,
            )
        ],
    )
    provider = FakeStructuredProvider(storyboard)
    director = DirectorAgent(provider=provider, model="test-model")

    plan = asyncio.run(director.run(project, script))

    assert plan.project is project
    assert plan.scenes[0].id == 1
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is DirectedStoryboard
    assert script.narration in provider.last_call["input_text"]
