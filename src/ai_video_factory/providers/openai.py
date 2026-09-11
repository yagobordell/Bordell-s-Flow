import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ai_video_factory.config import settings
from ai_video_factory.providers.base import StatefulStructuredResult

if TYPE_CHECKING:
    from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


class OpenAIProvider:
    """OpenAI implementation of the structured text provider contract."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: "AsyncOpenAI | Any | None" = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        fallback_service_tier: str | None = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)

        self._client = client
        self._reasoning_effort = reasoning_effort or settings.openai_reasoning_effort
        self._service_tier = service_tier or settings.openai_service_tier
        self._fallback_service_tier = (
            fallback_service_tier or settings.openai_fallback_service_tier
        )

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
    ) -> StructuredOutputT:
        response = await self._parse_with_flex_fallback(
            model=model,
            instructions=instructions,
            input=input_text,
            text_format=output_type,
        )

        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed structured output")

        if not isinstance(parsed, output_type):
            raise RuntimeError("OpenAI returned an unexpected structured output type")

        return parsed

    async def generate_structured_stateful[StructuredOutputT: BaseModel](
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[StructuredOutputT]:
        response = await self._parse_with_flex_fallback(
            model=model,
            instructions=instructions,
            input=input_text,
            text_format=output_type,
            previous_response_id=previous_response_id,
            store=True,
        )

        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed structured output")

        if not isinstance(parsed, output_type):
            raise RuntimeError("OpenAI returned an unexpected structured output type")

        response_id = getattr(response, "id", None)
        if not isinstance(response_id, str) or not response_id:
            raise RuntimeError("OpenAI returned no response id for stateful continuation")

        return StatefulStructuredResult(output=parsed, response_id=response_id)

    async def _parse_with_flex_fallback(self, **kwargs: Any) -> Any:
        request_kwargs = {
            **kwargs,
            "reasoning": {"effort": self._reasoning_effort},
            "service_tier": self._service_tier,
        }

        try:
            return await self._client.responses.parse(**request_kwargs)
        except Exception as exc:
            if not self._should_fallback_from_flex(exc):
                raise

        logger.warning(
            "OpenAI Flex returned HTTP 404; retrying once with service_tier=%s",
            self._fallback_service_tier,
        )
        request_kwargs["service_tier"] = self._fallback_service_tier
        return await self._client.responses.parse(**request_kwargs)

    def _should_fallback_from_flex(self, exc: Exception) -> bool:
        return (
            self._service_tier == "flex"
            and self._fallback_service_tier != "flex"
            and getattr(exc, "status_code", None) == 404
        )
