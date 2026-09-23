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


def test_narrative_block_bot_reconstructs_text_from_boundary_ids() -> None:
    source = SourceScript(text="Primera idea.\n\nSegunda frase.   Nuevo momento.")
    provider = FakeStructuredProvider(
        NarrativeBlocksOutput(block_end_unit_ids=[2, 3])
    )
    bot = NarrativeBlockBot(provider=provider, model="test-model")

    blocks = asyncio.run(bot.run(source))

    assert [block.model_dump() for block in blocks] == [
        {"id": 1, "text": "Primera idea. Segunda frase."},
        {"id": 2, "text": "Nuevo momento."},
    ]
    assert provider.last_call is not None
    assert provider.last_call["output_type"] is NarrativeBlocksOutput
    assert provider.last_call["input_text"] == (
        "1: Primera idea.\n2: Segunda frase.\n3: Nuevo momento."
    )


def test_narrative_block_bot_rejects_incomplete_boundaries() -> None:
    source = SourceScript(text="El samurái entra. Después desenvaina su espada.")
    provider = FakeStructuredProvider(
        NarrativeBlocksOutput(block_end_unit_ids=[1])
    )
    bot = NarrativeBlockBot(provider=provider, model="test-model")

    try:
        asyncio.run(bot.run(source))
    except ValueError as exc:
        assert "complete script" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_narrative_block_bot_rejects_duplicate_or_unordered_boundaries() -> None:
    source = SourceScript(text="Uno. Dos. Tres.")
    provider = FakeStructuredProvider(
        NarrativeBlocksOutput(block_end_unit_ids=[2, 2, 3])
    )
    bot = NarrativeBlockBot(provider=provider, model="test-model")

    try:
        asyncio.run(bot.run(source))
    except ValueError as exc:
        assert "duplicate or unordered" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
