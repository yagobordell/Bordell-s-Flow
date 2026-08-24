from typing import Protocol, TypeVar

from pydantic import BaseModel


StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)


class StructuredTextProvider(Protocol):
    """Provider contract for LLM calls that must return validated structured data."""

    async def generate_structured(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        output_type: type[StructuredOutputT],
    ) -> StructuredOutputT:
        """Generate and validate a Pydantic object from a text request."""
        ...
