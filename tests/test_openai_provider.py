import asyncio
from types import SimpleNamespace
from typing import Any

from ai_video_factory.domain import Script
from ai_video_factory.providers.openai import OpenAIProvider


class FakeResponses:
    def __init__(self, output_parsed: Script | None, *, response_id: str = "resp_test") -> None:
        self.output_parsed = output_parsed
        self.response_id = response_id
        self.last_call: dict[str, Any] | None = None

    async def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.last_call = kwargs
        return SimpleNamespace(output_parsed=self.output_parsed, id=self.response_id)


class FakeOpenAIClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


def test_openai_provider_uses_responses_parse() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected)
    provider = OpenAIProvider(client=FakeOpenAIClient(responses))  # type: ignore[arg-type]

    result = asyncio.run(
        provider.generate_structured(
            model="test-model",
            instructions="Write a short script",
            input_text="Tema: samuráis",
            output_type=Script,
        )
    )

    assert result == expected
    assert responses.last_call == {
        "model": "test-model",
        "instructions": "Write a short script",
        "input": "Tema: samuráis",
        "text_format": Script,
    }


def test_openai_provider_passes_previous_response_id_for_stateful_calls() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected, response_id="resp_2")
    provider = OpenAIProvider(client=FakeOpenAIClient(responses))  # type: ignore[arg-type]

    result = asyncio.run(
        provider.generate_structured_stateful(
            model="test-model",
            instructions="Keep continuity",
            input_text="Bloque 2",
            output_type=Script,
            previous_response_id="resp_1",
        )
    )

    assert result.output == expected
    assert result.response_id == "resp_2"
    assert responses.last_call == {
        "model": "test-model",
        "instructions": "Keep continuity",
        "input": "Bloque 2",
        "text_format": Script,
        "previous_response_id": "resp_1",
    }


def test_openai_provider_fails_when_output_is_missing() -> None:
    provider = OpenAIProvider(client=FakeOpenAIClient(FakeResponses(None)))  # type: ignore[arg-type]

    try:
        asyncio.run(
            provider.generate_structured(
                model="test-model",
                instructions="instructions",
                input_text="input",
                output_type=Script,
            )
        )
    except RuntimeError as exc:
        assert "no parsed structured output" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")
