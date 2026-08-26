import io
import wave
from pathlib import Path

from ai_video_factory.domain import NarrationAudio, SourceScript
from ai_video_factory.providers.speech import SpeechProvider


async def generate_narration_audio(
    source: SourceScript,
    *,
    speech_provider: SpeechProvider,
    output_dir: Path,
    model: str,
    voice: str,
    instructions: str,
    speed: float,
) -> NarrationAudio:
    """Generate one canonical narration WAV from the immutable source script."""

    if not source.text.strip():
        raise ValueError("Source script must contain non-whitespace narration text")
    if not model.strip():
        raise ValueError("Speech model must be non-empty")
    if not voice.strip():
        raise ValueError("Speech voice must be non-empty")
    if not 0.25 <= speed <= 4.0:
        raise ValueError("Speech speed must be between 0.25 and 4.0")

    generated = await speech_provider.generate_speech(
        text=source.text,
        model=model,
        voice=voice,
        instructions=instructions,
        speed=speed,
        output_format="wav",
    )

    if generated.extension != "wav":
        raise ValueError("Narration workflow requires WAV provider output")

    duration_seconds = _measure_wav_duration(generated.content)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "narration.wav"
    path.write_bytes(generated.content)

    return NarrationAudio(
        uri=path.relative_to(output_dir).as_posix(),
        duration_seconds=duration_seconds,
    )


def _measure_wav_duration(content: bytes) -> float:
    try:
        with wave.open(io.BytesIO(content), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            channel_count = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            declared_frame_count = wav_file.getnframes()

            if frame_rate <= 0 or channel_count <= 0 or sample_width <= 0:
                raise ValueError("Speech provider returned an invalid WAV format")
            if declared_frame_count <= 0:
                raise ValueError("Speech provider returned an empty or invalid WAV file")

            frame_data = wav_file.readframes(declared_frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("Speech provider returned invalid WAV data") from exc

    frame_size = channel_count * sample_width
    if not frame_data or len(frame_data) % frame_size != 0:
        raise ValueError("Speech provider returned incomplete WAV frame data")

    actual_frame_count = len(frame_data) // frame_size
    return actual_frame_count / frame_rate
