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
    FishSpeechBackend,
    FishSpeechParameters,
    FishSpeechTaskRunner,
    FishSpeechWorkerSettings,
    fish_speech_application_job_id,
)
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.fish_speech.model import _atempo_chain, split_narration_text


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
    assert backend.parameters.seed == 42
    assert backend.parameters.reference_profile == "project-narrator-v1"
    assert backend.parameters.reference_transcript == "Authorized reference transcript."


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



class BadOutputBackend(FakeBackend):
    def synthesize(self, *, parameters, reference_audio, output_path: Path) -> None:
        output_path.write_bytes(b"not-a-wave")


def test_fish_task_runner_rejects_bad_worker_wav(tmp_path: Path) -> None:
    backend = BadOutputBackend()
    runner = FishSpeechTaskRunner(backend=backend)
    request = InferenceJobRequest(
        job_id="fish-speech-bad-output",
        task=FISH_SPEECH_TASK,
        inputs=[],
        output=ObjectOutput(
            key="jobs/fish-speech-bad-output/narration.wav",
            content_type="audio/wav",
        ),
        parameters=_parameters(
            reference_profile=None,
            reference_audio_sha256=None,
            reference_transcript=None,
            reference_transcript_sha256=None,
        ),
    )

    with pytest.raises(RuntimeError, match="corrupt WAV"):
        runner.run(request, {}, tmp_path)


@pytest.mark.parametrize("speed", [0.25, 0.5, 1.0, 2.0, 4.0])
def test_fish_speed_atempo_chain_covers_full_neutral_range(speed: float) -> None:
    chain = _atempo_chain(speed)
    assert chain
    factors = [float(part.split("=", 1)[1]) for part in chain.split(",")]
    assert all(0.5 <= factor <= 2.0 for factor in factors)
    product = 1.0
    for factor in factors:
        product *= factor
    assert product == pytest.approx(speed)


def test_fish_bootstrap_marker_binds_model_and_runtime(tmp_path: Path) -> None:
    backend = FishSpeechBackend(model_root=tmp_path, device="cpu")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name in (
        "config.json",
        "model.safetensors.index.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
        "codec.pth",
        "tokenizer.json",
    ):
        (snapshot / name).write_bytes(b"x")

    (tmp_path / ".ready").write_text(
        (
            f"{FISH_SPEECH_MODEL_ID}@{FISH_SPEECH_MODEL_REVISION}|"
            f"runtime@{FISH_SPEECH_RUNTIME_COMMIT}"
        ),
        encoding="utf-8",
    )
    backend._validate_bootstrap()

    (tmp_path / ".ready").write_text("wrong@revision", encoding="utf-8")
    with pytest.raises(ModelBootstrapPendingError, match="does not match"):
        backend._validate_bootstrap()


def test_fish_job_identity_changes_with_seed() -> None:
    common = {
        "text": "Same narration.",
        "instructions": "Measured.",
        "speed": 1.0,
        "reference_profile": "voice-v1",
        "reference_audio_sha256": "a" * 64,
        "reference_transcript": "Hello.",
    }
    first = fish_speech_application_job_id(seed=42, **common)
    second = fish_speech_application_job_id(seed=43, **common)
    assert first != second
