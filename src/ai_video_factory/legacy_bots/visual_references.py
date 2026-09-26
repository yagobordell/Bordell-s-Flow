from pydantic import BaseModel, Field

from ai_video_factory.domain import ContinuityEntity, VisualReference
from ai_video_factory.providers.base import StructuredTextProvider
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramElementPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)

VISUAL_REFERENCE_INSTRUCTIONS = """\
Eres un bot de diseño de referencias visuales canónicas para un pipeline de vídeo.
Procesas exactamente una entidad de continuidad cada vez.

Tu tarea es convertir la identidad narrativa recibida en una descripción visual estable que pueda
reutilizarse para generar siempre la misma entidad en pasos posteriores.

La aplicación proporciona también el contexto narrativo de los bloques donde esa entidad está
activa. Usa ese contexto como evidencia para resolver época, entorno y significado visual, pero no
conviertas acciones temporales del guion en rasgos permanentes de la entidad.

Devuelve dos campos:
- description: identidad visual rica y estable de la entidad.
- safe_generation_description: descripción física breve y neutral destinada al generador de
  imágenes. Para localizaciones, debe contener solo geografía visible, arquitectura, materiales,
  distribución y landmarks físicos necesarios para reconocer el lugar. Evita estado narrativo,
  historia, peligrosidad, abandono, daño, conflicto, dramatización, personas concretas, acciones o
  acontecimientos. Para otros tipos puede ser equivalente a description.

Reglas estrictas:
- Devuelve ambas descripciones en inglés.
- Diseña siempre la referencia para un canvas cinematográfico horizontal 16:9. Debe leerse como
  una imagen widescreen real, nunca como un retrato vertical colocado dentro de un lienzo ancho.
- Aprovecha el eje horizontal, deja espacio lateral útil y evita sujetos pegados a los bordes.
- Para referencias con entorno, conserva profundidad legible de foreground, midground y background
  para que planos posteriores puedan usar parallax o desplazamientos de cámara.
- No añadas texto, UI, marcos, bordes, letterboxing ni composiciones tipo póster vertical.
- Conserva la identidad, tipo, nombre y significado de la entidad recibida.
- Puedes concretar detalles visuales moderados necesarios para hacer la entidad reconocible y
  consistente, siempre que no contradigan la entidad ni su contexto narrativo.
- No inventes hechos narrativos, relaciones, poderes, acciones, lugares ni historia adicional.
- No inventes texto escrito, logotipos, emblemas o símbolos concretos que no estén respaldados.
- Para personajes, prioriza rasgos visibles estables, ropa y accesorios; evita inventar datos
  personales precisos no respaldados.
- Para grupos, describe rasgos visuales compartidos y una representación coherente del conjunto.
- Para localizaciones, crea una única referencia física concreta y reutilizable coherente con el
  contexto narrativo. Prioriza arquitectura, materiales, distribución y rasgos permanentes.
- Si una localización es un país, ciudad, región u otro lugar muy amplio, no describas un catálogo
  geográfico, un collage de varias épocas ni una vista enciclopédica. Elige un único entorno físico
  representativo que encaje con el contexto narrativo.
- No adoptes por defecto una apariencia contemporánea si el contexto sitúa la acción en otra época.
- Para localizaciones, evita iluminación, clima o eventos temporales propios de un shot.
- Para objetos, describe únicamente propiedades físicas tangibles y reconocibles.
- No generes cámara, movimiento, acción, duración, transición ni instrucciones de vídeo.
- No incluyas Markdown ni explicaciones.
"""


class VisualDesignOutput(BaseModel):
    """Model-owned visual identity plus a generation-safe physical description."""

    description: str = Field(min_length=1)
    safe_generation_description: str = Field(min_length=1)


class VisualReferenceBot:
    """Build one canonical Ideogram reference caption from a continuity entity."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        entity: ContinuityEntity,
        *,
        visual_style: str,
        narrative_context: str,
        aspect_ratio: str = "16:9",
    ) -> VisualReference:
        style = " ".join(visual_style.split()).rstrip(" .")
        if not style:
            raise ValueError("VisualReferenceBot requires a non-empty visual style")

        context = " ".join(narrative_context.split())
        if not context:
            raise ValueError("VisualReferenceBot requires non-empty narrative context")
        if aspect_ratio.strip() != "16:9":
            raise ValueError("VisualReferenceBot production aspect ratio must be exactly 16:9")

        input_text = (
            f"ENTITY ID: {entity.id}\n"
            f"KIND: {entity.kind}\n"
            f"NAME: {entity.name}\n"
            f"CANONICAL DESCRIPTION: {entity.description}\n"
            f"PROJECT VISUAL STYLE: {style}\n"
            f"TARGET ASPECT RATIO: {aspect_ratio.strip()} horizontal cinematic landscape\n"
            f"NARRATIVE CONTEXT FOR THIS ENTITY:\n{context}"
        )
        result = await self._provider.generate_structured(
            model=self._model,
            instructions=VISUAL_REFERENCE_INSTRUCTIONS,
            input_text=input_text,
            output_type=VisualDesignOutput,
        )

        description = " ".join(result.description.split()).rstrip(" .")
        safe_description = " ".join(result.safe_generation_description.split()).rstrip(" .")
        if not description:
            raise ValueError("VisualReferenceBot returned an empty visual description")
        if not safe_description:
            raise ValueError("VisualReferenceBot returned an empty safe generation description")

        generation_description = safe_description if entity.kind == "location" else description
        prompt = _build_reference_prompt(
            kind=entity.kind,
            description=generation_description,
            visual_style=style,
            aspect_ratio=aspect_ratio.strip(),
        )
        return VisualReference(entity_id=entity.id, prompt=prompt)


def _build_reference_prompt(
    *,
    kind: str,
    description: str,
    visual_style: str,
    aspect_ratio: str,
) -> str:
    render_mode = _render_mode_for_style(visual_style)
    if render_mode == "photo":
        medium = "cinematic documentary reference photograph"
        render_description = (
            "realistic reference photography, natural proportions, crisp material detail"
        )
    else:
        medium = "digital image"
        render_description = visual_style

    style = IdeogramStylePlan(
        aesthetics=visual_style,
        lighting="even neutral reference lighting with clear readable form",
        medium=medium,
        render_mode=render_mode,
        render_description=render_description,
    )

    landscape_rule = (
        f"Native {aspect_ratio} horizontal cinematic composition using the full widescreen canvas, "
        "with useful lateral breathing room, no portrait framing, no borders and no letterboxing."
    )

    if kind == "character":
        high_level = (
            f"Canonical character reference. {description}. {landscape_rule} "
            "Full figure readable without filling the frame."
        )
        background = (
            "Simple neutral widescreen studio environment with clean lateral negative space, "
            "no text, UI or props."
        )
        elements = [
            IdeogramElementPlan(
                description=(
                    f"{description}. Neutral standing identity pose, clear silhouette, clothing "
                    "and recurring accessories fully visible, no action; occupy one side of the "
                    "wide frame while preserving generous lateral space."
                ),
                bbox=[100, 100, 900, 560],
            )
        ]
    elif kind == "group":
        high_level = f"Canonical group identity reference. {description}. {landscape_rule}"
        background = (
            "Simple neutral widescreen background with lateral breathing room and no text, "
            "labels, borders or temporary events."
        )
        elements = [
            IdeogramElementPlan(
                description=(
                    f"{description}. Small representative group arranged across the horizontal "
                    "frame, showing consistent shared appearance and recurring visual traits."
                ),
                bbox=[180, 80, 820, 920],
            )
        ]
    elif kind == "location":
        high_level = f"Canonical location reference. {description}. {landscape_rule}"
        background = (
            "Wide reusable environment reference emphasizing stable physical geography, "
            "architecture, materials, layout and recurring landmarks, with clear foreground, "
            "midground and background depth useful for parallax."
        )
        elements = []
    elif kind == "object":
        high_level = f"Canonical object reference. {description}. {landscape_rule}"
        background = (
            "Simple neutral widescreen studio background with useful lateral negative space, "
            "no text, labels, UI or borders."
        )
        elements = [
            IdeogramElementPlan(
                description=(
                    f"{description}. Single physical object fully visible with clear shape, "
                    "materials and recurring details, no action; keep ample horizontal space "
                    "around it for later shot composition."
                ),
                bbox=[180, 180, 820, 620],
            )
        ]
    else:
        raise ValueError(f"Unsupported continuity entity kind: {kind}")

    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=high_level,
            style=style,
            background=background,
            elements=elements,
        )
    )


def _render_mode_for_style(visual_style: str) -> str:
    style = visual_style.lower()
    non_photo_markers = (
        "illustration",
        "painting",
        "anime",
        "cartoon",
        "3d render",
        "stylized",
        "graphic",
    )
    if any(marker in style for marker in non_photo_markers):
        return "art"
    return "photo"
