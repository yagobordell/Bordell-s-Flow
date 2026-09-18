import hashlib
import wave
from pathlib import Path

import pytest

from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    ObjectInput,
    ObjectOutput,
)
from ai_video_factory.workers.fish_speech import (
    FISH_SPEECH_CHUNKING_PROFILE,
    FISH_SPEECH_GENERATION_PROFILE,
    FISH_SPEECH_MODEL_ID,
    FISH_SPEECH_MODEL_REVISION,
    FISH_SPEECH_RUNTIME_COMMIT,
    FISH_SPEECH_TASK,
    FishSpeechParameters,
    FishSpeechTaskRunner,
    FishSpeechWorkerSettings,
    fish_speech_application_job_id,
)
from ai_video_factory.workers.fish_speech.model import split_narration_text


class FakeBackend:
    def __init__(self) -> None:
        self.parameters = None
        self.reference_audio = None

    def prepare(self) -> None:
        pass

    def ready(self) -> None:
        pass

    def synthesize(self, *, parameters, reference_audio, output_path: Path) -> None:
        self.parameters = parameters
        self.reference_audio = reference_audio
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24_000)
            wav_file.writeframes(b"\x00\x00" * 2400)


def _parameters(**updates):
    transcript = "Authorized reference transcript."
    values = {
        "generation_profile": FISH_SPEECH_GENERATION_PROFILE,
        "chunking_profile": FISH_SPEECH_CHUNKING_PROFILE,
        "model_id": FISH_SPEECH_MODEL_ID,
        "model_revision": FISH_SPEECH_MODEL_REVISION,
        "runtime_commit": FISH_SPEECH_RUNTIME_COMMIT,
        "text": "A complete narration.",
        "instructions": "Measured documentary delivery.",
        "speed": 1.0,
        "seed": 42,
        "reference_profile": "project-narrator-v1",
        "reference_audio_sha256": "a" * 64,
        "reference_transcript": transcript,
        "reference_transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
    }
    values.update(updates)
    return values


def test_fish_settings_pin_model_revision_and_runtime(tmp_path: Path) -> None:
    settings = FishSpeechWorkerSettings(
        worker_mode="local",
        model_root=tmp_path,
    )
    assert settings.model_repository == FISH_SPEECH_MODEL_ID
    assert settings.model_revision == FISH_SPEECH_MODEL_REVISION
    assert settings.runtime_commit == FISH_SPEECH_RUNTIME_COMMIT


def test_fish_parameters_reject_mismatched_reference_transcript_hash() -> None:
    with pytest.raises(ValueError, match="transcript SHA-256"):
        FishSpeechParameters.model_validate(
            _parameters(reference_transcript_sha256="b" * 64)
        )


def test_fish_chunking_preserves_every_character_and_is_deterministic() -> None:
    text = (
        "First sentence is deliberately long enough to matter. "
        "Second sentence follows with more words. "
        "Third sentence closes the narration without truncation."
    )
    first = split_narration_text(text, 70)
    second = split_narration_text(text, 70)
    assert first == second
    assert "".join(first) == text
    assert len(first) > 1


def test_fish_application_job_id_includes_reference_and_speed() -> None:
    common = {
        "text": "Same narration.",
        "instructions": "Measured.",
        "seed": 42,
        "reference_profile": "voice-v1",
        "reference_audio_sha256": "a" * 64,
        "reference_transcript": "Hello.",
    }
    first = fish_speech_application_job_id(speed=1.0, **common)
    second = fish_speech_application_job_id(speed=1.1, **common)
    third = fish_speech_application_job_id(
        speed=1.0,
        **{**common, "reference_audio_sha256": "b" * 64},
    )
    assert first.startswith("fish-speech-")
    assert first != second
    assert first != third
    assert not first.startswith("breeze-")


def test_fish_task_runner_requires_reference_input_and_emits_canonical_wav(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    runner = FishSpeechTaskRunner(backend=backend)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    request = InferenceJobRequest(
        job_id="fish-speech-test",
        task=FISH_SPEECH_TASK,
        inputs=[
            ObjectInput(
                name="reference_audio",
                key="voices/project-narrator-v1.wav",
                sha256="a" * 64,
                content_type="audio/wav",
            )
        ],
        output=ObjectOutput(
            key="jobs/fish-speech-test/narration.wav",
            content_type="audio/wav",
        ),
        parameters=_parameters(),
    )

    artifact = runner.run(request, {"reference_audio": reference}, tmp_path)

    assert artifact.content_type == "audio/wav"
    assert artifact.path.is_file()
    with wave.open(str(artifact.path), "rb") as wav_file:
        assert wav_file.getframerate() == 24_000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
    assert backend.reference_audio == reference


def test_fish_task_runner_rejects_unexpected_inputs(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = FishSpeechTaskRunner(backend=backend)
    parameters = _parameters(
        reference_profile=None,
        reference_audio_sha256=None,
        reference_transcript=None,
        reference_transcript_sha256=None,
    )
    request = InferenceJobRequest(
        job_id="fish-speech-test",
        task=FISH_SPEECH_TASK,
        inputs=[],
        output=ObjectOutput(
            key="jobs/fish-speech-test/narration.wav",
            content_type="audio/wav",
        ),
        parameters=parameters,
    )
    unexpected = tmp_path / "unexpected.wav"
    unexpected.write_bytes(b"x")

    with pytest.raises(ValueError, match="do not accept object inputs"):
        runner.run(request, {"unexpected": unexpected}, tmp_path)
