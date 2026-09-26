from pydantic import BaseModel, Field

from ai_video_factory.domain import Beat, NarrationWord, SourceScript
from ai_video_factory.providers.base import StructuredTextProvider

BEAT_TIMING_INSTRUCTIONS = """\
Eres un bot de alineación narrativa para un vídeo corto.

Recibes el guion canónico, los beats narrativos en orden y las palabras reconocidas de la
narración con IDs y timestamps.

Tu única tarea es indicar qué palabra termina cada beat.

Reglas estrictas:
- Devuelve exactamente un `beat_end_word_id` por beat, en el mismo orden de los beats.
- Los IDs devueltos deben ser estrictamente crecientes.
- Cada beat debe recibir al menos una palabra.
- La última palabra del último beat debe ser la última palabra disponible de la narración.
- Usa el guion canónico como fuente de verdad semántica.
- Usa las palabras reconocidas solo como evidencia de alineación temporal.
- Tolera pequeños errores de transcripción, puntuación o símbolos extraños sin alterar el guion.
- Elige fronteras semánticas naturales: una palabra no puede pertenecer a dos beats.
- No inventes palabras, beats ni IDs.
- No generes timestamps; la aplicación los reconstruirá de forma determinista.
- Devuelve únicamente la salida estructurada solicitada.
"""


class BeatWordBoundariesOutput(BaseModel):
    """Model-owned semantic boundary: final narration word ID for each beat."""

    beat_end_word_ids: list[int] = Field(min_length=1)


class BeatTimingBot:
    """Choose semantic word boundaries for ordered beats."""

    def __init__(self, *, provider: StructuredTextProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def run(
        self,
        source: SourceScript,
        beats: list[Beat],
        words: list[NarrationWord],
    ) -> list[int]:
        if not beats:
            raise ValueError("BeatTimingBot requires at least one beat")
        if not words:
            raise ValueError("BeatTimingBot requires at least one narration word")

        beat_lines = "\n".join(f"{beat.id}: {beat.action}" for beat in beats)
        word_lines = "\n".join(
            f"{word.id}: [{word.start_seconds:.3f}-{word.end_seconds:.3f}] {word.text}"
            for word in words
        )
        input_text = (
            "GUION CANÓNICO:\n"
            f"{source.text}\n\n"
            "BEATS:\n"
            f"{beat_lines}\n\n"
            "PALABRAS RECONOCIDAS:\n"
            f"{word_lines}"
        )

        result = await self._provider.generate_structured(
            model=self._model,
            instructions=BEAT_TIMING_INSTRUCTIONS,
            input_text=input_text,
            output_type=BeatWordBoundariesOutput,
        )

        ends = result.beat_end_word_ids
        if len(ends) != len(beats):
            raise ValueError("BeatTimingBot must return exactly one boundary per beat")
        if any(left >= right for left, right in zip(ends, ends[1:], strict=False)):
            raise ValueError("BeatTimingBot boundaries must be strictly increasing")

        valid_word_ids = {word.id for word in words}
        if any(word_id not in valid_word_ids for word_id in ends):
            raise ValueError("BeatTimingBot returned an unknown narration word ID")
        if ends[-1] != words[-1].id:
            raise ValueError("BeatTimingBot must end the last beat on the final narration word")

        return ends
