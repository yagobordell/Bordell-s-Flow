from pydantic import BaseModel, Field

from ai_video_factory.domain import Beat, Scene
from ai_video_factory.providers.base import StructuredTextProvider


SCENE_PLANNER_INSTRUCTIONS = """\
Eres un bot de planificación narrativa.
Tu única tarea es agrupar beats consecutivos en escenas coherentes.

Una escena es una unidad narrativa que puede compartir un mismo momento, espacio o continuidad de acción.

Reglas estrictas:
- Agrupa únicamente beats consecutivos.
- Conserva exactamente el orden de los beats.
- Cada beat debe aparecer exactamente una vez.
- No inventes, elimines ni dupliques beats.
- No generes personajes, escenarios, planos, cámara, duración, estilo ni prompts visuales.
- Devuelve únicamente grupos de IDs de beats, uno por escena.
"""


class SceneGroupsOutput(BaseModel):
    """Minimal model-owned output: ordered groups of existing beat IDs."""

    scenes: list[list[int]] = Field(min_length=1)


class ScenePlannerBot:
    """Group consecutive beats into minimal scene contracts."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, beats: list[Beat]) -> list[Scene]:
        if not beats:
            raise ValueError("ScenePlannerBot requires at least one beat")

        beat_lines = "\n".join(
            f"{beat.id}: [block {beat.block_id}] {beat.action}" for beat in beats
        )
        result = await self._provider.generate_structured(
            model=self._model,
            instructions=SCENE_PLANNER_INSTRUCTIONS,
            input_text=beat_lines,
            output_type=SceneGroupsOutput,
        )

        groups = result.scenes
        if any(not group for group in groups):
            raise ValueError("ScenePlannerBot returned an empty scene")

        expected_ids = [beat.id for beat in beats]
        returned_ids = [beat_id for group in groups for beat_id in group]
        if returned_ids != expected_ids:
            raise ValueError("ScenePlannerBot must use every beat exactly once and preserve order")

        return [Scene(id=index, beat_ids=group) for index, group in enumerate(groups, start=1)]
