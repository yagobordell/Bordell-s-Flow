from typing import Literal

from pydantic import BaseModel, Field

from ai_video_factory.domain import ContinuityEntity, NarrativeBlock
from ai_video_factory.providers.base import (
    StatefulStructuredResult,
    StatefulStructuredTextProvider,
)


CONTINUITY_INSTRUCTIONS = """\
Eres un bot de continuidad visual para un guion ya terminado.
Procesas exactamente un bloque narrativo cada vez, en orden, manteniendo el contexto de los bloques anteriores.

La aplicación te proporciona un registro canónico de entidades ya conocidas. Ese registro es la fuente de verdad.

Reglas estrictas:
- Reutiliza un ID existente cuando el bloque vuelva a referirse a la misma entidad visual.
- Crea una entidad nueva solo cuando aparezca una persona, grupo, lugar u objeto visualmente relevante que deba poder mantenerse consistente en pasos posteriores.
- No inventes nombres propios, rasgos físicos, objetos, lugares ni relaciones que el guion no sostenga.
- Las descripciones deben ser breves, estables y útiles para reconocer la misma entidad más adelante.
- No modifiques ni sustituyas entidades ya registradas.
- `existing_entity_ids` solo puede contener IDs presentes en el registro canónico recibido.
- `new_entities` no debe incluir IDs; la aplicación los asignará de forma determinista.
- Devuelve únicamente decisiones de continuidad. No generes escenas, shots, cámara, iluminación, prompts de imagen ni explicaciones.
"""


class NewContinuityEntity(BaseModel):
    """Model-owned description of a newly observed visual entity."""

    kind: Literal["character", "group", "location", "object"]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ContinuityDecision(BaseModel):
    """Model-owned continuity decision for one narrative block."""

    existing_entity_ids: list[str] = Field(default_factory=list)
    new_entities: list[NewContinuityEntity] = Field(default_factory=list)


class ContinuityBot:
    """Resolve recurring visual entities while preserving provider-managed conversation state."""

    def __init__(self, *, provider: StatefulStructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        block: NarrativeBlock,
        *,
        known_entities: list[ContinuityEntity],
        previous_response_id: str | None,
    ) -> StatefulStructuredResult[ContinuityDecision]:
        registry = _format_registry(known_entities)
        input_text = (
            f"REGISTRO CANÓNICO ACTUAL:\n{registry}\n\n"
            f"BLOQUE NARRATIVO {block.id}:\n{block.text}"
        )

        result = await self._provider.generate_structured_stateful(
            model=self._model,
            instructions=CONTINUITY_INSTRUCTIONS,
            input_text=input_text,
            output_type=ContinuityDecision,
            previous_response_id=previous_response_id,
        )

        existing_ids = result.output.existing_entity_ids
        if len(existing_ids) != len(set(existing_ids)):
            raise ValueError("ContinuityBot returned duplicate existing entity IDs")

        known_ids = {entity.id for entity in known_entities}
        unknown_ids = [entity_id for entity_id in existing_ids if entity_id not in known_ids]
        if unknown_ids:
            raise ValueError(
                "ContinuityBot referenced unknown entity IDs: " + ", ".join(unknown_ids)
            )

        return result


def _format_registry(entities: list[ContinuityEntity]) -> str:
    if not entities:
        return "(vacío)"

    return "\n".join(
        f"- {entity.id} | {entity.kind} | {entity.name} | {entity.description}"
        for entity in entities
    )
