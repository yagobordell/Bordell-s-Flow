from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class StatefulStructuredResult[StructuredOutputT: BaseModel]:
    """Validated structured output together with the response state identifier."""

    output: StructuredOutputT
    response_id: str


class StructuredTextProvider(Protocol):
    """Provider contract for LLM calls that must return validated structured data."""

    async def generate_structured[StructuredOutputT: BaseModel](
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
    ) -> StructuredOutputT:
        """Generate and validate a Pydantic object from a text request."""
        ...


class StatefulStructuredTextProvider(StructuredTextProvider, Protocol):
    """Structured provider that can continue from a previous model response."""

    async def generate_structured_stateful[StructuredOutputT: BaseModel](
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[StructuredOutputT]:
        """Generate structured data while carrying forward provider-managed state."""
        ...
