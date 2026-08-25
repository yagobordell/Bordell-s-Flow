import re

from pydantic import BaseModel, Field

from ai_video_factory.domain import NarrativeBlock, SourceScript
from ai_video_factory.providers.base import StructuredTextProvider


NARRATIVE_BLOCK_INSTRUCTIONS = """\
Eres un bot de segmentación narrativa.
Tu única tarea es agrupar unidades consecutivas de un guion ya terminado en bloques narrativos.

Recibirás el guion dividido por la aplicación en unidades numeradas e inmutables.
Un bloque narrativo agrupa unidades consecutivas que desarrollan la misma idea, momento o unidad del relato.

Reglas estrictas:
- No reescribas ni devuelvas el texto del guion.
- Devuelve únicamente el ID de la última unidad de cada bloque narrativo.
- Los IDs deben estar en orden estrictamente creciente.
- La última unidad del guion debe cerrar el último bloque.
- No omitas, dupliques ni reordenes unidades.
- No generes escenas, beats, personajes, prompts visuales ni explicaciones.
"""


class NarrativeBlocksOutput(BaseModel):
    """Model-owned output: only the boundary of each narrative block."""

    block_end_unit_ids: list[int] = Field(min_length=1)


class NarrativeBlockBot:
    """Group immutable source units into contiguous semantic blocks."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, source: SourceScript) -> list[NarrativeBlock]:
        units = _split_units(source.text)
        numbered_script = "\n".join(
            f"{index}: {text}" for index, text in enumerate(units, start=1)
        )

        result = await self._provider.generate_structured(
            model=self._model,
            instructions=NARRATIVE_BLOCK_INSTRUCTIONS,
            input_text=numbered_script,
            output_type=NarrativeBlocksOutput,
        )

        ends = result.block_end_unit_ids
        if ends != sorted(set(ends)):
            raise ValueError("NarrativeBlockBot returned duplicate or unordered block boundaries")
        if ends[0] < 1 or ends[-1] > len(units):
            raise ValueError("NarrativeBlockBot returned a block boundary outside the script")
        if ends[-1] != len(units):
            raise ValueError("NarrativeBlockBot did not cover the complete script")

        blocks: list[NarrativeBlock] = []
        start = 0
        for block_id, end in enumerate(ends, start=1):
            text = " ".join(units[start:end])
            blocks.append(NarrativeBlock(id=block_id, text=text))
            start = end

        return blocks


def _split_units(text: str) -> list[str]:
    """Split normalized script wording into immutable sentence-like units."""

    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("Source script is empty")

    units = [
        part.strip()
        for part in re.split(
            r"(?:(?<=[.!?…])|(?<=[.!?…][\"”»']))\s+",
            normalized,
        )
        if part.strip()
    ]
    return units or [normalized]
