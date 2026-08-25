import asyncio
from typing import Any

from pydantic import BaseModel

from ai_video_factory.bots.narrative_blocks import NarrativeBlockBot, NarrativeBlocksOutput
from ai_video_factory.domain import SourceScript


class FakeStructuredProvider:
    def __init__(self, result: BaseModel) -> None:
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


def test_narrative_block_bot_assigns_ids_without_model_generated_metadata() -> None:
    source = SourceScript(text="Primera idea. Segunda frase. Nuevo momento.")
    provider = FakeStructuredProvider(
        NarrativeBlocksOutput(blocks=["Primera idea. Segunda frase.", "Nuevo momento."])
    )
    bot = NarrativeBlockBot(provider=provider, model="test-model")

    blocks = asyncio.run(bot.run(source))

    assert [block.model_dump() for block in blocks] == [
        {"id": 1, "text": "Primera idea. Segunda frase."},
        {"id": 2, "text": "Nuevo momento."},
    ]
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is NarrativeBlocksOutput


def test_narrative_block_bot_rejects_rewritten_script() -> None:
    source = SourceScript(text="El samurái entra. Después desenvaina su espada.")
    provider = FakeStructuredProvider(
        NarrativeBlocksOutput(blocks=["El guerrero entra.", "Después desenvaina su espada."])
    )
    bot = NarrativeBlockBot(provider=provider, model="test-model")

    try:
        asyncio.run(bot.run(source))
    except ValueError as exc:
        assert "changed, omitted, duplicated, or reordered" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
