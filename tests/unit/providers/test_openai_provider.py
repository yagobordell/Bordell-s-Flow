import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ai_video_factory.providers.openai import OpenAIProvider


class StructuredResult(BaseModel):
    title: str
    hook: str
    narration: str


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeResponses:
    def __init__(
        self,
        output_parsed: StructuredResult | None,
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


def _provider_without_rate_limit_wait(responses: FakeResponses) -> OpenAIProvider:
    return OpenAIProvider(
        client=FakeOpenAIClient(responses),  # type: ignore[arg-type]
        reasoning_effort="high",
        service_tier="flex",
        fallback_service_tier="default",
        rate_limit_retries=0,
        rate_limit_backoff_seconds=0,
    )


def test_openai_provider_uses_high_reasoning_and_flex() -> None:
    expected = StructuredResult(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected)
    provider = _provider(responses)

    result = asyncio.run(
        provider.generate_structured(
            model="test-model",
            instructions="Write a short script",
            input_text="Tema: samuráis",
            output_type=StructuredResult,
        )
    )

    assert result == expected
    assert responses.last_call == {
        "model": "test-model",
        "instructions": "Write a short script",
        "input": "Tema: samuráis",
        "text_format": StructuredResult,
        "reasoning": {"effort": "high"},
        "service_tier": "flex",
    }


def test_openai_provider_retries_once_with_default_when_flex_returns_404() -> None:
    expected = StructuredResult(title="Demo", hook="Hook", narration="Hook. Narración.")
    responses = FakeResponses(expected, failures=[FakeStatusError(404)])
    provider = _provider(responses)

    result = asyncio.run(
        provider.generate_structured(
            model="test-model",
            instructions="Write a short script",
            input_text="Tema: samuráis",
            output_type=StructuredResult,
        )
    )

    assert result == expected
    assert len(responses.calls) == 2
    assert responses.calls[0]["service_tier"] == "flex"
    assert responses.calls[1]["service_tier"] == "default"
    assert responses.calls[0]["reasoning"] == {"effort": "high"}
    assert responses.calls[1]["reasoning"] == {"effort": "high"}


def test_openai_provider_uses_default_tier_when_flex_is_rate_limited() -> None:
    expected = StructuredResult(title="Demo", hook="Hook", narration="Hook. NarraciÃ³n.")
    responses = FakeResponses(expected, failures=[FakeStatusError(429)])
    provider = _provider_without_rate_limit_wait(responses)

    result = asyncio.run(
        provider.generate_structured(
            model="test-model",
            instructions="Write a short script",
            input_text="Tema: samurÃ¡is",
            output_type=StructuredResult,
        )
    )

    assert result == expected
    assert len(responses.calls) == 2
    assert responses.calls[0]["service_tier"] == "flex"
    assert responses.calls[1]["service_tier"] == "default"


def test_openai_provider_does_not_fallback_for_other_status_codes() -> None:
    responses = FakeResponses(None, failures=[FakeStatusError(500)])
    provider = _provider(responses)

    with pytest.raises(FakeStatusError, match="HTTP 500"):
        asyncio.run(
            provider.generate_structured(
                model="test-model",
                instructions="instructions",
                input_text="input",
                output_type=StructuredResult,
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
                output_type=StructuredResult,
            )
        )


def test_opt_in_metered_provider_returns_original_usage_bearing_response() -> None:
    expected = StructuredResult(title="Demo", hook="Hook", narration="Texto.")
    usage = SimpleNamespace(
        input_tokens=125,
        input_tokens_details=SimpleNamespace(cached_tokens=25, cache_write_tokens=0),
        output_tokens=38,
        output_tokens_details=SimpleNamespace(reasoning_tokens=12),
    )
    response = SimpleNamespace(
        output_parsed=expected,
        id="resp_metered",
        model="gpt-6-luna",
        service_tier="default",
        usage=usage,
    )
    calls: list[dict[str, Any]] = []

    async def parse(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return response

    provider = OpenAIProvider(
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),  # type: ignore[arg-type]
        reasoning_effort="medium",
        service_tier="default",
    )

    parsed, raw_response = asyncio.run(
        provider.generate_structured_with_response(
            model="gpt-6-luna",
            instructions="Segment blocks",
            input_text='{"plain_script_for_recording":"Texto."}',
            output_type=StructuredResult,
        )
    )

    assert parsed is expected
    assert raw_response is response
    assert raw_response.usage is usage
    assert raw_response.usage.output_tokens_details.reasoning_tokens == 12
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert calls[0]["service_tier"] == "default"
