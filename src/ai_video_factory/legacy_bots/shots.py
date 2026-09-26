from pydantic import BaseModel, Field

from ai_video_factory.domain import Beat, ContinuityEntity, Scene
from ai_video_factory.providers.base import (
    StatefulStructuredResult,
    StatefulStructuredTextProvider,
)

SHOT_PLANNER_INSTRUCTIONS = """\
Eres un bot de planificación de shots para un vídeo corto.
Procesas exactamente una escena cada vez y las escenas llegan en orden narrativo.

Tu tarea es dividir los beats consecutivos de la escena en shots visuales simples y coherentes.
La aplicación te proporciona las entidades canónicas disponibles para esta escena.

Reglas estrictas:
- Usa cada beat exactamente una vez y conserva su orden.
- Cada shot debe contener uno o más beats consecutivos.
- Combina beats consecutivos cuando puedan representarse como una misma acción visual continua.
- Separa shots cuando cambien de forma significativa la acción, el sujeto, el lugar o el momento.
- No fuerces un shot distinto para cada beat si no aporta una separación visual real.
- `entity_ids` solo puede contener IDs de las entidades canónicas recibidas.
- Incluye únicamente las entidades que deben verse o participar realmente en ese shot.
- `action` describe en una frase breve la acción visual principal del shot.
- No crees entidades nuevas ni cambies la identidad de las existentes.
- No inventes hechos que el beat no sostenga.
- No generes IDs de shot; la aplicación los asignará.
- No generes cámara, tipo de plano, lente, iluminación ni duración.
- No generes transición, estilo ni prompts de imagen o vídeo.
- Devuelve únicamente la planificación estructurada solicitada.
"""


class PlannedShot(BaseModel):
    """Model-owned shot decision before deterministic application IDs are assigned."""

    beat_ids: list[int] = Field(min_length=1)
    entity_ids: list[str]
    action: str = Field(min_length=1)


class ShotPlanOutput(BaseModel):
    """Ordered shot decisions for exactly one scene."""

    shots: list[PlannedShot] = Field(min_length=1)


class ShotPlannerBot:
    """Plan one scene at a time while carrying semantic shot context forward."""

    def __init__(self, *, provider: StatefulStructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        scene: Scene,
        *,
        beats: list[Beat],
        available_entities: list[ContinuityEntity],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[ShotPlanOutput]:
        expected_beat_ids = scene.beat_ids
        received_beat_ids = [beat.id for beat in beats]
        if received_beat_ids != expected_beat_ids:
            raise ValueError(
                "ShotPlannerBot beats must exactly match the scene beat IDs and order"
            )

        beat_lines = "\n".join(f"{beat.id}: {beat.action}" for beat in beats)
        registry = _format_registry(available_entities)
        input_text = (
            f"ESCENA {scene.id}\n\n"
            f"BEATS:\n{beat_lines}\n\n"
            f"ENTIDADES CANÓNICAS DISPONIBLES:\n{registry}"
        )

        result = await self._provider.generate_structured_stateful(
            model=self._model,
            instructions=SHOT_PLANNER_INSTRUCTIONS,
            input_text=input_text,
            output_type=ShotPlanOutput,
            previous_response_id=previous_response_id,
        )

        returned_beat_ids = [
            beat_id for shot in result.output.shots for beat_id in shot.beat_ids
        ]
        if returned_beat_ids != expected_beat_ids:
            raise ValueError(
                "ShotPlannerBot must use every scene beat exactly once and preserve order"
            )

        available_ids = {entity.id for entity in available_entities}
        for shot in result.output.shots:
            if len(shot.entity_ids) != len(set(shot.entity_ids)):
                raise ValueError("ShotPlannerBot returned duplicate entity IDs inside a shot")
            unknown_ids = [
                entity_id
                for entity_id in shot.entity_ids
                if entity_id not in available_ids
            ]
            if unknown_ids:
                raise ValueError(
                    "ShotPlannerBot referenced unavailable entity IDs: "
                    + ", ".join(unknown_ids)
                )

        return result


def _format_registry(entities: list[ContinuityEntity]) -> str:
    if not entities:
        return "(vacío)"

    return "\n".join(
        f"- {entity.id} | {entity.kind} | {entity.name} | {entity.description}"
        for entity in entities
    )
