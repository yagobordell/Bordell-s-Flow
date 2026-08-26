from pydantic import BaseModel, Field

from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame, VisualReference
from ai_video_factory.providers.base import StructuredTextProvider

STORYBOARD_FRAME_INSTRUCTIONS = """\
Eres un bot de storyboard para un vídeo documental vertical.

Recibes un shot ya planificado, su duración real y las referencias visuales canónicas de las
entidades que participan. Cuando existe, también recibes el prompt del frame anterior.

Tu única tarea es escribir en inglés un prompt provider-neutral para UN solo keyframe estático que
represente visualmente el núcleo del shot.

Reglas estrictas:
- Conserva la identidad visual de las entidades usando las referencias canónicas como restricciones.
- Las referencias canónicas describen identidad, NO obligan a conservar su fondo neutro, pose de
  referencia o iluminación de estudio.
- El keyframe debe representar de forma visible el núcleo de `SHOT.action`; no sustituyas la acción
  por un establishing shot genérico si el sujeto o relación principal puede mostrarse en imagen.
- Si la acción se centra en una entidad participante, hazla visualmente relevante en el frame salvo
  que la propia acción describa explícitamente su ausencia.
- Usa el prompt anterior solo para mantener continuidad visible cuando el shot actual comparte
  entidades o entorno; no arrastres elementos que ya no pertenecen al shot.
- Continuidad no significa repetición. Si cambia el significado narrativo respecto al frame anterior,
  introduce una variación visual significativa en sujeto, estado, composición o contexto.
- La duración sirve para limitar la complejidad visual: representa un momento claro que pueda
  sostener el shot, no una secuencia de acciones comprimida en una sola imagen.
- Puedes decidir composición, escala de plano, ángulo de cámara estático y distribución espacial.
- No generes movimiento de cámara, transición, duración, instrucciones de vídeo ni múltiples
  paneles.
- No conviertas el keyframe en collage, split screen, hoja de contactos o storyboard grid.
- No añadas texto visible, subtítulos, labels, logos ni watermarks.
- No inventes personajes, objetos, símbolos escritos ni hechos narrativos ajenos a la acción del
  shot.
- Mantén el estilo visual solicitado y el aspect ratio indicado.
- Devuelve únicamente el prompt, sin Markdown ni explicaciones.
"""


class StoryboardPromptOutput(BaseModel):
    """Model-owned visual description for one storyboard still."""

    prompt: str = Field(min_length=1)


class StoryboardFrameBot:
    """Design one provider-neutral storyboard keyframe prompt at a time."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        shot: Shot,
        timing: ShotTiming,
        references: list[VisualReference],
        *,
        visual_style: str,
        aspect_ratio: str,
        previous_frame: StoryboardFrame | None,
    ) -> str:
        if timing.shot_id != shot.id:
            raise ValueError("StoryboardFrameBot requires matching shot and timing IDs")
        if not visual_style.strip():
            raise ValueError("Storyboard visual style must be non-empty")
        if not aspect_ratio.strip():
            raise ValueError("Storyboard aspect ratio must be non-empty")

        duration = timing.end_seconds - timing.start_seconds
        if duration <= 0:
            raise ValueError("Storyboard shot duration must be positive")

        reference_text = "\n".join(
            f"{reference.entity_id}: {reference.prompt}" for reference in references
        ) or "(none)"
        previous_text = previous_frame.prompt if previous_frame is not None else "(none)"
        input_text = (
            "SHOT:\n"
            f"id: {shot.id}\n"
            f"scene_id: {shot.scene_id}\n"
            f"action: {shot.action}\n"
            f"duration_seconds: {duration:.3f}\n"
            f"visual_style: {visual_style}\n"
            f"aspect_ratio: {aspect_ratio}\n\n"
            "CANONICAL VISUAL REFERENCES:\n"
            f"{reference_text}\n\n"
            "PREVIOUS STORYBOARD FRAME:\n"
            f"{previous_text}"
        )

        result = await self._provider.generate_structured(
            model=self._model,
            instructions=STORYBOARD_FRAME_INSTRUCTIONS,
            input_text=input_text,
            output_type=StoryboardPromptOutput,
        )
        return result.prompt.strip()
