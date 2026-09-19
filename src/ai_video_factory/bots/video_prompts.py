from pydantic import BaseModel, Field

from ai_video_factory.domain import Shot, ShotTiming, StoryboardFrame, VideoPrompt
from ai_video_factory.providers.base import StructuredTextProvider

VIDEO_PROMPT_INSTRUCTIONS = """\
Eres un bot de motion planning para un vídeo documental cinematográfico horizontal 16:9
generado desde un keyframe.

Recibes un shot ya planificado, su duración real, el prompt del storyboard que define el estado
visual inicial y, cuando existe, el prompt de vídeo del shot anterior dentro de la misma escena.

Tu única tarea es escribir en inglés un prompt provider-neutral para UN solo clip de vídeo continuo
que anime el keyframe inicial y represente visualmente el núcleo del shot.

Reglas estrictas:
- Trata el storyboard como el estado visual inicial canónico. Conserva identidad, vestuario,
  entorno, objetos y relaciones espaciales que sigan siendo relevantes para el shot.
- Haz que `SHOT.action` ocurra de forma visible durante el clip. El movimiento debe aportar cambio
  narrativo sin limitarse a micro-movimientos genéricos si la acción exige una transformación clara.
- Mantén la composición cinematográfica landscape 16:9 del keyframe y evita movimientos que
  empujen sujetos importantes fuera de los márgenes seguros.
- Describe solo movimiento útil: movimiento del sujeto, del entorno y de cámara cuando ayude a
  expresar la acción. No es obligatorio usar los tres tipos.
- La duración real limita la complejidad. Un shot corto debe tener una acción simple y legible; no
  comprimas una cadena larga de eventos en pocos segundos.
- El clip debe ser un solo plano continuo. No añadas cortes, transiciones, montajes, flashbacks,
  split screens, múltiples ángulos consecutivos ni cambios de escena.
- Usa el prompt anterior únicamente como contexto de continuidad de movimiento dentro de la misma
  escena. Continuidad no significa repetir la misma animación ni arrastrar acciones ya terminadas.
- No inventes personajes, objetos, lugares, texto visible, logos, subtítulos, hechos narrativos ni
  elementos que no estén apoyados por `SHOT.action` o por el storyboard inicial.
- No describas diálogo, voz en off, música ni diseño sonoro. El audio canónico se compone en una
  etapa posterior.
- Evita instrucciones específicas de proveedor o modelo: no menciones LTX, seeds, frames, FPS,
  cuantización, checkpoints, resolución de render ni parámetros de inferencia.
- Mantén el estilo visual y el aspect ratio solicitados, pero céntrate en el cambio temporal y no
  reescribas innecesariamente toda la descripción estática del keyframe.
- Devuelve únicamente el prompt, sin Markdown ni explicaciones.
"""


class VideoPromptOutput(BaseModel):
    """Model-owned temporal motion description for one canonical shot."""

    prompt: str = Field(min_length=1)


class VideoPromptBot:
    """Plan provider-neutral motion for one storyboard-conditioned video shot."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        shot: Shot,
        timing: ShotTiming,
        storyboard_frame: StoryboardFrame,
        *,
        visual_style: str,
        aspect_ratio: str,
        previous_prompt: VideoPrompt | None,
    ) -> str:
        if timing.shot_id != shot.id:
            raise ValueError("VideoPromptBot requires matching shot and timing IDs")
        if storyboard_frame.shot_id != shot.id:
            raise ValueError("VideoPromptBot requires matching shot and storyboard frame IDs")
        if not visual_style.strip():
            raise ValueError("Video visual style must be non-empty")
        if not aspect_ratio.strip():
            raise ValueError("Video aspect ratio must be non-empty")

        duration = timing.end_seconds - timing.start_seconds
        if duration <= 0:
            raise ValueError("Video shot duration must be positive")

        previous_text = previous_prompt.prompt if previous_prompt is not None else "(none)"
        input_text = (
            "SHOT:\n"
            f"id: {shot.id}\n"
            f"scene_id: {shot.scene_id}\n"
            f"action: {shot.action}\n"
            f"duration_seconds: {duration:.3f}\n"
            f"visual_style: {visual_style}\n"
            f"aspect_ratio: {aspect_ratio}\n\n"
            "STARTING STORYBOARD KEYFRAME:\n"
            f"{storyboard_frame.prompt}\n\n"
            "PREVIOUS VIDEO PROMPT:\n"
            f"{previous_text}"
        )

        result = await self._provider.generate_structured(
            model=self._model,
            instructions=VIDEO_PROMPT_INSTRUCTIONS,
            input_text=input_text,
            output_type=VideoPromptOutput,
        )
        return result.prompt.strip()
