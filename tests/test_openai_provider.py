import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.domain import Script
from ai_video_factory.providers.openai import OpenAIProvider


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeResponses:
    def __init__(
        self,
        output_parsed: Script | None,
        *,
        response_id: str = "resp_test",
        failures: list[Exception] | None = None,
    ) -> None:
        self.output_parsed = output_parsed
        self.response_id = response_id
        self.failures = list(failures or [])
        self.calls: list[dict[str, Any]] = []

    @property
    def last_call(self) -> dict[str, Any] | None:
        if not self.calls:
            return None
        return self.calls[-1]

    async def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(output_parsed=self.output_parsed, id=self.response_id)


class FakeOpenAIClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses


def _provider(responses: FakeResponses) -> OpenAIProvider:
    return OpenAIProvider(
        client=FakeOpenAIClient(responses),  # type: ignore[arg-type]
        reasoning_effort="high",
        service_tier="flex",
        fallback_service_tier="default",
    )


def test_openai_provider_uses_high_reasoning_and_flex() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected)
    provider = _provider(responses)

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
        "reasoning": {"effort": "high"},
        "service_tier": "flex",
    }


def test_openai_provider_passes_previous_response_id_for_stateful_calls() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected, response_id="resp_2")
    provider = _provider(responses)

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
        "store": True,
        "reasoning": {"effort": "high"},
        "service_tier": "flex",
    }


def test_openai_provider_retries_once_with_default_when_flex_returns_404() -> None:
    expected = Script(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected, failures=[FakeStatusError(404)])
    provider = _provider(responses)

    result = asyncio.run(
        provider.generate_structured(
            model="test-model",
            instructions="Write a short script",
            input_text="Tema: samuráis",
            output_type=Script,
        )
    )

    assert result == expected
    assert len(responses.calls) == 2
    assert responses.calls[0]["service_tier"] == "flex"
    assert responses.calls[1]["service_tier"] == "default"
    assert responses.calls[0]["reasoning"] == {"effort": "high"}
    assert responses.calls[1]["reasoning"] == {"effort": "high"}


def test_openai_provider_does_not_fallback_for_other_status_codes() -> None:
    responses = FakeResponses(None, failures=[FakeStatusError(500)])
    provider = _provider(responses)

    with pytest.raises(FakeStatusError, match="HTTP 500"):
        asyncio.run(
            provider.generate_structured(
                model="test-model",
                instructions="instructions",
                input_text="input",
                output_type=Script,
            )
        )

    assert len(responses.calls) == 1
    assert responses.calls[0]["service_tier"] == "flex"


def test_openai_provider_fails_when_output_is_missing() -> None:
    provider = _provider(FakeResponses(None))

    with pytest.raises(RuntimeError, match="no parsed structured output"):
        asyncio.run(
            provider.generate_structured(
                model="test-model",
                instructions="instructions",
                input_text="input",
                output_type=Script,
            )
        )
