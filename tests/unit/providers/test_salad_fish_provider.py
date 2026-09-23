import asyncio
import hashlib
import io
import wave
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobResponse, OutputArtifact
from ai_video_factory.providers.salad_fish_speech import (
    FishSpeechReference,
    SaladFishSpeechProvider,
)
from ai_video_factory.workers.fish_speech import (
    FISH_SPEECH_MODEL_ID,
    FISH_SPEECH_TASK,
)


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


class FakeExecutor:
    def __init__(self, payload: bytes, *, replayed: bool = False) -> None:
        self.payload = payload
        self.replayed = replayed
        self.request = None
        self.metadata = None

    def execute(self, request, *, metadata):
        self.request = request
        self.metadata = metadata
        return InferenceJobResponse(
            job_id=request.job_id,
            request_sha256=request.fingerprint(),
            output=OutputArtifact(
                key=request.output.key,
                content_type="audio/wav",
                size_bytes=len(self.payload),
                sha256=hashlib.sha256(self.payload).hexdigest(),
            ),
            attempt_count=1,
            replayed=self.replayed,
        )

    def download_output(self, response, destination: Path) -> None:
        destination.write_bytes(self.payload)


def _reference() -> FishSpeechReference:
    return FishSpeechReference(
        profile="project-narrator-v1",
        audio_key="voices/project-narrator-v1.wav",
        audio_sha256="a" * 64,
        transcript="This is the authorized narrator reference.",
    )


def test_salad_fish_provider_builds_dedicated_job_contract(tmp_path: Path) -> None:
    executor = FakeExecutor(_wav_bytes())
    provider = SaladFishSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        reference=_reference(),
        seed=42,
    )

    speech = asyncio.run(
        provider.generate_speech(
            text="Canonical narration.",
            model=FISH_SPEECH_MODEL_ID,
            voice="project-narrator-v1",
            instructions="Measured delivery.",
            speed=1.0,
            output_format="wav",
        )
    )

    assert executor.request is not None
    assert executor.request.task == FISH_SPEECH_TASK
    assert executor.request.job_id.startswith("fish-speech-")
    assert executor.request.inputs[0].name == "reference_audio"
    assert executor.request.inputs[0].key == "voices/project-narrator-v1.wav"
    assert executor.request.parameters["reference_profile"] == "project-narrator-v1"
    assert executor.metadata == {
        "phase": "5",
        "provider": "fish_speech",
        "reference-profile": "project-narrator-v1",
    }
    assert speech.metadata["provider"] == "fish_speech"
    assert speech.metadata["sample_rate"] == 24_000
    assert speech.metadata["channels"] == 1


def test_salad_fish_provider_surfaces_replay_without_new_identity(tmp_path: Path) -> None:
    executor = FakeExecutor(_wav_bytes(), replayed=True)
    provider = SaladFishSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        reference=_reference(),
    )

    speech = asyncio.run(
        provider.generate_speech(
            text="Replay this narration.",
            model=FISH_SPEECH_MODEL_ID,
            voice="project-narrator-v1",
            instructions="Measured delivery.",
            speed=1.0,
            output_format="wav",
        )
    )

    assert speech.metadata["replayed"] is True


def test_salad_fish_provider_identity_changes_with_reference(tmp_path: Path) -> None:
    executor = FakeExecutor(_wav_bytes())
    first = SaladFishSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        reference=_reference(),
    )
    asyncio.run(
        first.generate_speech(
            text="Same narration.",
            model=FISH_SPEECH_MODEL_ID,
            voice="project-narrator-v1",
            instructions="Measured.",
            speed=1.0,
            output_format="wav",
        )
    )
    first_id = executor.request.job_id

    alternate = FishSpeechReference(
        profile="project-narrator-v2",
        audio_key="voices/project-narrator-v2.wav",
        audio_sha256="b" * 64,
        transcript="Another authorized narrator reference.",
    )
    second = SaladFishSpeechProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        reference=alternate,
    )
    asyncio.run(
        second.generate_speech(
            text="Same narration.",
            model=FISH_SPEECH_MODEL_ID,
            voice="project-narrator-v2",
            instructions="Measured.",
            speed=1.0,
            output_format="wav",
        )
    )
    assert first_id != executor.request.job_id
