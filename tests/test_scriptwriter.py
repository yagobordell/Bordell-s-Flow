import asyncio
from typing import Any

from pydantic import BaseModel

from ai_video_factory.agents.scriptwriter import ScriptWriterAgent
from ai_video_factory.domain import ProjectConfig, Script


class FakeStructuredProvider:
    def __init__(self, result: Script) -> None:
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


def test_scriptwriter_returns_structured_script() -> None:
    expected = Script(
        title="La historia de los samuráis",
        hook="Los samuráis no nacieron siendo la élite guerrera de Japón.",
        narration=(
            "Los samuráis no nacieron siendo la élite guerrera de Japón. "
            "Su historia empezó mucho antes de convertirse en un símbolo mundial."
        ),
    )
    provider = FakeStructuredProvider(expected)
    agent = ScriptWriterAgent(provider=provider, model="test-model")
    project = ProjectConfig(topic="La historia de los samuráis")

    result = asyncio.run(agent.run(project))

    assert result == expected
    assert provider.last_call is not None
    assert provider.last_call["model"] == "test-model"
    assert provider.last_call["output_type"] is Script


def test_scriptwriter_passes_project_context_to_provider() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    provider = FakeStructuredProvider(expected)
    agent = ScriptWriterAgent(provider=provider, model="test-model")
    project = ProjectConfig(
        topic="Una nueva bebida energética",
        language="es",
        duration_seconds=30,
        audience="jóvenes adultos",
        style="energetic commercial",
    )

    asyncio.run(agent.run(project))

    assert provider.last_call is not None
    prompt = provider.last_call["input_text"]
    assert "Una nueva bebida energética" in prompt
    assert "30 segundos" in prompt
    assert "75 palabras" in prompt
    assert "jóvenes adultos" in prompt
    assert "energetic commercial" in prompt
