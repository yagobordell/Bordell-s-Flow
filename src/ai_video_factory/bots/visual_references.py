from pydantic import BaseModel, Field

from ai_video_factory.domain import ContinuityEntity, VisualReference
from ai_video_factory.providers.base import StructuredTextProvider

VISUAL_REFERENCE_INSTRUCTIONS = """\
Eres un bot de diseño de referencias visuales canónicas para un pipeline de vídeo.
Procesas exactamente una entidad de continuidad cada vez.

Tu tarea es convertir la identidad narrativa recibida en una descripción visual estable que pueda
reutilizarse para generar siempre la misma entidad en pasos posteriores.

Reglas estrictas:
- Devuelve la descripción visual en inglés.
- Conserva la identidad, tipo, nombre y significado de la entidad recibida.
- Puedes concretar detalles visuales moderados necesarios para hacer la entidad reconocible y
  consistente, siempre que no contradigan la información recibida.
- No inventes hechos narrativos, relaciones, poderes, acciones, lugares ni historia adicional.
- No inventes texto escrito, logotipos, emblemas o símbolos concretos que no estén respaldados.
- Para personajes, prioriza rasgos visibles estables, ropa y accesorios; evita inventar datos
  personales precisos no respaldados.
- Para grupos, describe rasgos visuales compartidos y una representación coherente del conjunto.
- Para localizaciones, describe arquitectura, materiales, distribución y rasgos permanentes; evita
  iluminación, clima o eventos temporales propios de un shot.
- Para objetos, describe únicamente propiedades físicas tangibles y reconocibles.
- No generes cámara, movimiento, acción, duración, transición ni instrucciones de vídeo.
- No incluyas Markdown ni explicaciones.
"""


class VisualDesignOutput(BaseModel):
    """Model-owned visual identity before the fixed application template is applied."""

    description: str = Field(min_length=1)


class VisualReferenceBot:
    """Build one provider-neutral canonical reference prompt from a continuity entity."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        entity: ContinuityEntity,
        *,
        visual_style: str,
    ) -> VisualReference:
        style = visual_style.strip()
        if not style:
            raise ValueError("VisualReferenceBot requires a non-empty visual style")

        input_text = (
            f"ENTITY ID: {entity.id}\n"
            f"KIND: {entity.kind}\n"
            f"NAME: {entity.name}\n"
            f"CANONICAL DESCRIPTION: {entity.description}\n"
            f"PROJECT VISUAL STYLE: {style}"
        )
        result = await self._provider.generate_structured(
            model=self._model,
            instructions=VISUAL_REFERENCE_INSTRUCTIONS,
            input_text=input_text,
            output_type=VisualDesignOutput,
        )

        description = " ".join(result.description.split())
        if not description:
            raise ValueError("VisualReferenceBot returned an empty visual description")

        prompt = _build_reference_prompt(
            kind=entity.kind,
            description=description,
            visual_style=style,
        )
        return VisualReference(entity_id=entity.id, prompt=prompt)


def _build_reference_prompt(*, kind: str, description: str, visual_style: str) -> str:
    templates = {
        "character": (
            "Canonical character reference, {style}. {description}. "
            "Single full-body subject in a neutral standing pose, clear silhouette, clothing and "
            "recurring accessories fully visible, plain neutral studio background, even reference "
            "lighting, no action, no text, no labels, no watermark."
        ),
        "group": (
            "Canonical group reference, {style}. {description}. "
            "Small representative group showing consistent shared appearance and recurring visual "
            "traits, neutral arrangement, plain neutral background, even reference lighting, "
            "no action, no text, no labels, no watermark."
        ),
        "location": (
            "Canonical location reference, {style}. {description}. "
            "Clear unobstructed environment view emphasizing permanent architecture, materials, "
            "layout and recurring landmarks, neutral reference lighting, no temporary events, "
            "no text, no labels, no watermark."
        ),
        "object": (
            "Canonical object reference, {style}. {description}. "
            "Single physical object isolated and fully visible, clear shape, materials and "
            "recurring details, plain neutral background, even reference lighting, no action, "
            "no text, no labels, no watermark."
        ),
    }
    try:
        template = templates[kind]
    except KeyError as exc:
        raise ValueError(f"Unsupported continuity entity kind: {kind}") from exc

    return template.format(style=visual_style, description=description)
