import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ai_video_factory.config import settings

if TYPE_CHECKING:
    from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

_OPENAI_RATE_LIMIT_STATUS = 429
_DEFAULT_RATE_LIMIT_RETRIES = 4
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 5.0


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
        rate_limit_retries: int = _DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_backoff_seconds: float = _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
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
        if rate_limit_retries < 0:
            raise ValueError("OpenAI rate_limit_retries must be non-negative")
        if rate_limit_backoff_seconds < 0:
            raise ValueError("OpenAI rate_limit_backoff_seconds must be non-negative")
        self._rate_limit_retries = rate_limit_retries
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds

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

    async def generate_structured_with_response[StructuredOutputT: BaseModel](
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
    ) -> tuple[StructuredOutputT, Any]:
        """Return validated structured output and its original, usage-bearing API response.

        Opt-in for metered pipelines. API usage is returned unchanged; estimated
        token counts never replace the provider's reported usage.
        """
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
        return parsed, response

    async def _parse_with_flex_fallback(self, **kwargs: Any) -> Any:
        request_kwargs = {
            **kwargs,
            "reasoning": {"effort": self._reasoning_effort},
            "service_tier": self._service_tier,
        }

        try:
            # A 429 on Flex is commonly a temporary Flex-capacity limit. Move to
            # the configured default tier immediately instead of spending the
            # whole retry budget against the same saturated tier.
            return await self._parse_with_rate_limit_retries(
                request_kwargs,
                retry_rate_limit=False,
            )
        except Exception as exc:
            if not self._should_fallback_from_flex(exc):
                raise
            logger.warning(
                "OpenAI Flex returned HTTP %s; retrying with service_tier=%s",
                getattr(exc, "status_code", "an error"),
                self._fallback_service_tier,
            )
            request_kwargs["service_tier"] = self._fallback_service_tier
            return await self._parse_with_rate_limit_retries(request_kwargs)

    async def _parse_with_rate_limit_retries(
        self,
        request_kwargs: dict[str, Any],
        *,
        retry_rate_limit: bool = True,
    ) -> Any:
        attempts = 0
        while True:
            try:
                return await self._client.responses.parse(**request_kwargs)
            except Exception as exc:
                if (
                    not retry_rate_limit
                    or not _is_rate_limit_error(exc)
                    or attempts >= self._rate_limit_retries
                ):
                    raise
                delay = _rate_limit_delay(
                    exc,
                    attempt=attempts,
                    base_seconds=self._rate_limit_backoff_seconds,
                )
                attempts += 1
                logger.warning(
                    "OpenAI rate limit received; retrying attempt=%d/%d in %.1fs",
                    attempts,
                    self._rate_limit_retries,
                    delay,
                )
                await asyncio.sleep(delay)

    def _should_fallback_from_flex(self, exc: Exception) -> bool:
        return (
            self._service_tier == "flex"
            and self._fallback_service_tier != "flex"
            and getattr(exc, "status_code", None) in {404, _OPENAI_RATE_LIMIT_STATUS}
        )


def _is_rate_limit_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == _OPENAI_RATE_LIMIT_STATUS


def _rate_limit_delay(exc: Exception, *, attempt: int, base_seconds: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = headers.get("retry-after") if headers is not None else None
    try:
        if retry_after is not None:
            return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        pass
    return base_seconds * (2**attempt)
