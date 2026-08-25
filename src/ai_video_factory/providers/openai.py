from typing import TYPE_CHECKING, Any

from ai_video_factory.providers.base import StatefulStructuredResult, StructuredOutputT

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class OpenAIProvider:
    """OpenAI implementation of the structured text provider contract."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: "AsyncOpenAI | Any | None" = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key)

        self._client = client

    async def generate_structured(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
    ) -> StructuredOutputT:
        response = await self._client.responses.parse(
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

    async def generate_structured_stateful(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[StructuredOutputT]:
        response = await self._client.responses.parse(
            model=model,
            instructions=instructions,
            input=input_text,
            text_format=output_type,
            previous_response_id=previous_response_id,
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
