import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectInput, ObjectOutput
from ai_video_factory.workers.whisper import (
    WHISPER_GENERATION_PROFILE,
    WHISPER_MODEL_ID,
    WHISPER_TRANSCRIPTION_TASK,
    TransformersWhisperBackend,
    WhisperTaskRunner,
    WhisperTranscriptionParameters,
    WhisperWorkerSettings,
    WhisperWord,
    whisper_application_job_id,
)
import ai_video_factory.workers.whisper.model as whisper_model


class FakeBackend:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.ready_calls = 0
        self.calls: list[dict[str, Any]] = []

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1

    def transcribe(self, **kwargs: Any) -> list[WhisperWord]:
        self.calls.append(kwargs)
        return [
            WhisperWord(text="Hello", start_seconds=0.1, end_seconds=0.5),
            WhisperWord(text="world", start_seconds=0.6, end_seconds=1.0),
        ]


def _request() -> InferenceJobRequest:
    job_id = "whisper-test-job"
    return InferenceJobRequest(
        job_id=job_id,
        task=WHISPER_TRANSCRIPTION_TASK,
        inputs=[
            ObjectInput(
                name="audio",
                key="phase5/whisper/inputs/audio.wav",
                sha256="a" * 64,
                content_type="audio/wav",
            )
        ],
        output=ObjectOutput(
            key=f"jobs/{job_id}/words.json",
            content_type="application/json",
        ),
        parameters={
            "generation_profile": WHISPER_GENERATION_PROFILE,
            "model_id": WHISPER_MODEL_ID,
            "prompt": "Hello world",
            "language": "en",
        },
    )


def test_whisper_application_job_id_is_deterministic() -> None:
    first = whisper_application_job_id(
        audio_sha256="a" * 64,
        prompt="Hello world",
        language="en",
    )
    second = whisper_application_job_id(
        audio_sha256="a" * 64,
        prompt="Hello world",
        language="en",
    )
    changed = whisper_application_job_id(
        audio_sha256="a" * 64,
        prompt="Different prompt",
        language="en",
    )

    assert first == second
    assert first.startswith("whisper-")
    assert changed != first


def test_whisper_task_runner_writes_word_timestamp_json(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = WhisperTaskRunner(backend=backend)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav")

    artifact = runner.run(_request(), {"audio": audio}, tmp_path / "work")

    assert artifact.content_type == "application/json"
    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": "1",
        "model_id": WHISPER_MODEL_ID,
        "words": [
            {"text": "Hello", "start_seconds": 0.1, "end_seconds": 0.5},
            {"text": "world", "start_seconds": 0.6, "end_seconds": 1.0},
        ],
    }
    assert backend.calls[0]["audio_path"] == audio


def test_whisper_backend_builds_once_and_requests_word_timestamps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model_root = tmp_path / "model"
    model_root.mkdir()
    (model_root / "config.json").write_text("{}", encoding="utf-8")
    state: dict[str, Any] = {"builds": 0, "calls": []}

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeTorch:
        cuda = FakeCuda()
        float16 = "float16"

    class FakePromptIds:
        def to(self, device: str) -> str:
            return f"prompt-ids:{device}"

    class FakeTokenizer:
        def get_prompt_ids(self, prompt: str, *, return_tensors: str) -> FakePromptIds:
            state["prompt"] = (prompt, return_tensors)
            return FakePromptIds()

    class FakePipeline:
        tokenizer = FakeTokenizer()

        def __call__(self, audio: str, **kwargs: Any) -> dict[str, Any]:
            state["calls"].append((audio, kwargs))
            return {
                "chunks": [
                    {"text": " Hello ", "timestamp": (0.0, 0.4)},
                    {"text": "world", "timestamp": (0.5, 0.9)},
                ]
            }

    def fake_pipeline_factory(**kwargs: Any) -> FakePipeline:
        state["builds"] += 1
        state["pipeline_kwargs"] = kwargs
        return FakePipeline()

    monkeypatch.setattr(
        whisper_model,
        "_load_whisper_bindings",
        lambda: whisper_model._WhisperBindings(
            torch=FakeTorch,
            pipeline_factory=fake_pipeline_factory,
        ),
    )

    backend = TransformersWhisperBackend(model_root=model_root)
    backend.prepare()
    backend.prepare()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav")
    words = backend.transcribe(
        audio_path=audio,
        parameters=WhisperTranscriptionParameters(
            generation_profile=WHISPER_GENERATION_PROFILE,
            model_id=WHISPER_MODEL_ID,
            prompt="Canonical narration",
            language="en",
        ),
    )

    assert state["builds"] == 1
    assert state["pipeline_kwargs"]["model"] == str(model_root)
    assert state["pipeline_kwargs"]["device"] == "cuda:0"
    assert state["calls"][0][1]["return_timestamps"] == "word"
    assert state["calls"][0][1]["generate_kwargs"] == {
        "task": "transcribe",
        "language": "en",
        "prompt_ids": "prompt-ids:cuda:0",
    }
    assert state["prompt"] == ("Canonical narration", "pt")
    assert [word.text for word in words] == ["Hello", "world"]


def test_whisper_worker_settings_and_salad_manifest() -> None:
    settings = WhisperWorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_lease_seconds=60,
        worker_heartbeat_seconds=10,
    )
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["whisper"]

    assert settings.model_repository == WHISPER_MODEL_ID
    assert settings.device == "cuda:0"
    assert service["queue_name"] == "ai-video-factory-whisper-jobs"
    assert service["group_name"] == "ai-video-factory-whisper-worker"
    assert service["dockerfile"] == "docker/workers/whisper/Dockerfile"
    assert service["autoscaler"]["min_replicas"] == 0
    assert "HF_TOKEN" not in service["required_secrets"]


def test_whisper_container_is_model_specific() -> None:
    dockerfile = Path("docker/workers/whisper/Dockerfile")
    text = dockerfile.read_text(encoding="utf-8")
    entrypoint = Path("docker/workers/whisper/entrypoint.sh").read_text(encoding="utf-8")

    assert "COPY src /opt/factory/src" in text
    assert "COPY . /opt/factory" not in text
    assert "transformers==5.5.2" in text
    assert "ai_video_factory.workers.whisper.runtime:app" in entrypoint
