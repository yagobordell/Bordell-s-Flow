from pydantic import BaseModel, Field

from ai_video_factory.domain import ProjectConfig, Script, StoryboardScene, VideoPlan
from ai_video_factory.providers.base import StructuredTextProvider

DIRECTOR_INSTRUCTIONS = """\
Eres el director de un vídeo documental cinematográfico horizontal 16:9.
Transforma un guion narrado en un plan audiovisual estructurado pensado para landscape widescreen.

Reglas:
- Divide la narración en escenas breves y visualmente claras.
- Conserva el contenido y el orden del guion; no reescribas la historia.
- Cada escena debe contener una porción continua de la narración.
- Escribe los prompts visuales en inglés para maximizar compatibilidad con modelos de imagen.
- Mantén coherencia visual entre escenas: época, personajes, vestuario, iluminación y estilo.
- Compón para 16:9 horizontal: aprovecha el eje lateral, deja aire para movimiento y evita
  encuadres de retrato o sujetos apretados contra los bordes.
- Evita texto visible, logos, marcas de agua, bordes, letterboxing y elementos UI.
- Usa movimientos de cámara simples y realizables para image-to-video.
- La suma aproximada de las duraciones debe acercarse a la duración objetivo del proyecto.
- `visual_style` debe describir la identidad visual global del vídeo de forma reutilizable.
"""


class DirectedStoryboard(BaseModel):
    """Model-owned portion of the experimental Phase 1 VideoPlan."""

    title: str
    visual_style: str
    scenes: list[StoryboardScene] = Field(min_length=1)


class DirectorAgent:
    """Experimental Phase 1 director kept as an optional compatibility utility."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, project: ProjectConfig, script: Script) -> VideoPlan:
        input_text = (
            f"Tema: {project.topic}\n"
            f"Idioma de narración: {project.language}\n"
            f"Duración objetivo: {project.duration_seconds} segundos\n"
            f"Formato: {project.aspect_ratio}\n"
            f"Público: {project.audience}\n"
            f"Estilo solicitado: {project.style}\n\n"
            f"Título del guion: {script.title}\n"
            f"Hook: {script.hook}\n"
            f"Narración completa:\n{script.narration}"
        )

        storyboard = await self._provider.generate_structured(
            model=self._model,
            instructions=DIRECTOR_INSTRUCTIONS,
            input_text=input_text,
            output_type=DirectedStoryboard,
        )

        return VideoPlan(
            project=project,
            title=storyboard.title,
            visual_style=storyboard.visual_style,
            scenes=storyboard.scenes,
        )
