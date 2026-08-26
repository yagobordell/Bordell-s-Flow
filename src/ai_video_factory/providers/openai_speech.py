from typing import TYPE_CHECKING, Any

from ai_video_factory.providers.speech import GeneratedSpeech, SpeechFormat

if TYPE_CHECKING:
    from openai import AsyncOpenAI


class OpenAISpeechProvider:
    """OpenAI implementation of the provider-neutral speech generation contract."""

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

    async def generate_speech(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        response = await self._client.audio.speech.create(
            model=model,
            input=text,
            voice=voice,
            instructions=instructions,
            response_format=output_format,
            speed=speed,
        )

        content = getattr(response, "content", None)
        if not isinstance(content, bytes) or not content:
            raise RuntimeError("OpenAI speech generation returned no binary audio payload")

        return GeneratedSpeech(
            content=content,
            media_type="audio/wav",
            extension=output_format,
        )
