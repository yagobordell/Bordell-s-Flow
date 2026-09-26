from pydantic import BaseModel, Field

from ai_video_factory.domain import NarrativeBlock
from ai_video_factory.providers.base import StructuredTextProvider

BEAT_EXTRACTOR_INSTRUCTIONS = """\
Eres un bot de análisis narrativo.
Tu única tarea es convertir un bloque narrativo en una secuencia de beats.

Un beat es una sola acción, cambio, revelación o idea claramente visualizable dentro del bloque.

Reglas estrictas:
- Mantén el orden del bloque.
- No inventes información que no esté presente o implícita en el texto.
- Cada beat debe expresar una sola acción, cambio o idea.
- Usa frases breves y concretas.
- No generes IDs, escenas, planos, cámara, estilo, personajes adicionales ni prompts visuales.
- Devuelve únicamente la lista de acciones de los beats.
"""


class BeatActionsOutput(BaseModel):
    """Minimal model-owned output: only beat actions."""

    actions: list[str] = Field(min_length=1)


class BeatExtractorBot:
    """Extract ordered visualizable beats from one narrative block."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, block: NarrativeBlock) -> list[str]:
        result = await self._provider.generate_structured(
            model=self._model,
            instructions=BEAT_EXTRACTOR_INSTRUCTIONS,
            input_text=block.text,
            output_type=BeatActionsOutput,
        )

        actions = [action.strip() for action in result.actions]
        if any(not action for action in actions):
            raise ValueError(f"BeatExtractorBot returned an empty beat for block {block.id}")

        return actions
