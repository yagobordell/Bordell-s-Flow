from ai_video_factory.domain import ProjectConfig, Script
from ai_video_factory.providers.base import StructuredTextProvider


SCRIPTWRITER_INSTRUCTIONS = """\
Eres el guionista de vídeos verticales cortos para redes sociales.
Tu objetivo es crear una narración clara, dinámica y fácil de locutar.

Reglas:
- Escribe en el idioma solicitado.
- Abre con un hook fuerte que despierte curiosidad inmediatamente.
- El campo `narration` debe contener el guion completo y comenzar exactamente con el hook.
- Prioriza frases cortas y naturales para voz en off.
- Evita introducciones genéricas, relleno y despedidas innecesarias.
- No incluyas indicaciones visuales, acotaciones, Markdown ni etiquetas dentro de la narración.
- Ajusta aproximadamente la longitud a la duración objetivo indicada por el usuario.
"""


class ScriptWriterAgent:
    """Creates the narrative script from a project topic."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(self, project: ProjectConfig) -> Script:
        target_words = round(project.duration_seconds * 2.5)
        input_text = (
            f"Tema: {project.topic}\n"
            f"Idioma: {project.language}\n"
            f"Público: {project.audience}\n"
            f"Duración objetivo: {project.duration_seconds} segundos\n"
            f"Longitud orientativa: {target_words} palabras (aprox.)\n"
            f"Estilo general del proyecto: {project.style}"
        )

        return await self._provider.generate_structured(
            model=self._model,
            instructions=SCRIPTWRITER_INSTRUCTIONS,
            input_text=input_text,
            output_type=Script,
        )
