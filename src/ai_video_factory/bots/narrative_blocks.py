from pydantic import BaseModel, Field

from ai_video_factory.domain import NarrativeBlock, SourceScript
from ai_video_factory.providers.base import StructuredTextProvider


NARRATIVE_BLOCK_INSTRUCTIONS = """\
Eres un bot de segmentación narrativa.
Tu única tarea es dividir un guion ya terminado en bloques narrativos contiguos.

Un bloque narrativo agrupa frases que desarrollan la misma idea, momento o unidad del relato.

Reglas estrictas:
- No resumas, reescribas, corrijas ni añadas texto.
- Conserva exactamente el contenido y el orden del guion.
- Cada fragmento del guion debe aparecer una sola vez.
- Los bloques deben ser contiguos; nunca mezcles partes alejadas del guion.
- No generes escenas, beats, personajes, prompts visuales ni explicaciones.
- Devuelve únicamente la lista de textos de los bloques.
"""


class NarrativeBlocksOutput(BaseModel):
    """Minimal model-owned output: only the block texts."""

    blocks: list[str] = Field(min_length=1)


class NarrativeBlockBot:
    """Split a source script into contiguous semantic blocks."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, source: SourceScript) -> list[NarrativeBlock]:
        result = await self._provider.generate_structured(
            model=self._model,
            instructions=NARRATIVE_BLOCK_INSTRUCTIONS,
            input_text=source.text,
            output_type=NarrativeBlocksOutput,
        )

        texts = [text.strip() for text in result.blocks]
        if any(not text for text in texts):
            raise ValueError("NarrativeBlockBot returned an empty block")

        if _normalize(" ".join(texts)) != _normalize(source.text):
            raise ValueError("NarrativeBlockBot changed, omitted, duplicated, or reordered script text")

        return [NarrativeBlock(id=index, text=text) for index, text in enumerate(texts, start=1)]


def _normalize(text: str) -> str:
    """Normalize whitespace only; narrative wording and punctuation must remain unchanged."""

    return " ".join(text.split())
