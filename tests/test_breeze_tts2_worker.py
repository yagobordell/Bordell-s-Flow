import json
import wave
from pathlib import Path
from typing import Any

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.workers.breeze_tts2 import (
    BREEZE_TTS2_GENERATION_PROFILE,
    BREEZE_TTS2_MODEL_ID,
    BREEZE_TTS2_TASK,
    BreezeSpeechTaskRunner,
    BreezeTTS2WorkerSettings,
    breeze_application_job_id,
)
from ai_video_factory.workers.breeze_tts2.model import _atempo_chain, _split_narration_text


class FakeBackend:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.ready_calls = 0
        self.calls: list[dict[str, Any]] = []

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1

    def synthesize(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        output = kwargs["output_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24_000)
            wav_file.writeframes(b"\x00\x00" * 2400)


def _request() -> InferenceJobRequest:
    job_id = "breeze-test-job"
    return InferenceJobRequest(
        job_id=job_id,
        task=BREEZE_TTS2_TASK,
        output=ObjectOutput(
            key=f"jobs/{job_id}/narration.wav",
            content_type="audio/wav",
        ),
        parameters={
            "generation_profile": BREEZE_TTS2_GENERATION_PROFILE,
            "model_id": BREEZE_TTS2_MODEL_ID,
            "text": "The narration begins here.",
            "voice": "A warm documentary narrator.",
            "instructions": "Measured delivery.",
            "speed": 1.0,
            "cfg_scale": 4.0,
            "seed": 42,
        },
    )


def test_breeze_application_job_id_is_deterministic() -> None:
    kwargs = {
        "text": "The narration begins here.",
        "voice": "A warm documentary narrator.",
        "instructions": "Measured delivery.",
        "speed": 1.0,
        "cfg_scale": 4.0,
        "seed": 42,
    }
    first = breeze_application_job_id(**kwargs)
    second = breeze_application_job_id(**kwargs)
    changed = breeze_application_job_id(**{**kwargs, "seed": 43})

    assert first == second
    assert first.startswith("breeze-")
    assert changed != first


def test_breeze_task_runner_writes_wav_artifact(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = BreezeSpeechTaskRunner(backend=backend)

    artifact = runner.run(_request(), {}, tmp_path / "work")

    assert artifact.content_type == "audio/wav"
    assert artifact.path.name == "narration.wav"
    assert artifact.path.stat().st_size > 44
    assert backend.calls[0]["parameters"].model_id == BREEZE_TTS2_MODEL_ID


def test_breeze_text_chunking_preserves_content_order() -> None:
    text = "First sentence is short. Second sentence is also short. Third sentence ends here."
    chunks = _split_narration_text(text, 35)

    assert len(chunks) >= 2
    assert " ".join(chunks) == text
    assert all(len(chunk) <= 35 for chunk in chunks)


def test_breeze_atempo_chain_covers_full_supported_speed_range() -> None:
    assert _atempo_chain(1.0) == "atempo=1"
    assert _atempo_chain(4.0) == "atempo=2,atempo=2"
    assert _atempo_chain(0.25) == "atempo=0.5,atempo=0.5"


def test_breeze_worker_settings_and_salad_manifest() -> None:
    settings = BreezeTTS2WorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_lease_seconds=60,
        worker_heartbeat_seconds=10,
    )
    document = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = document["services"]["breeze_tts2"]

    assert settings.model_repository == BREEZE_TTS2_MODEL_ID
    assert settings.device == "cuda"
    assert settings.max_chunk_chars == 1200
    assert service["queue_name"] == "ai-video-factory-breeze-tts2-jobs"
    assert service["resources"]["gpu_class_names"] == ["RTX 4090"]
    assert service["dockerfile"] == "docker/workers/breeze-tts2/Dockerfile"
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 2


def test_breeze_container_pins_runtime_and_targets_4090() -> None:
    dockerfile = Path("docker/workers/breeze-tts2/Dockerfile").read_text(encoding="utf-8")
    entrypoint = Path("docker/workers/breeze-tts2/entrypoint.sh").read_text(encoding="utf-8")

    assert "BREEZE_RUNTIME_COMMIT=008f769016b0a24711becd7a4925030bc93f608c" in dockerfile
    assert "FLASH_ATTN_CUDA_ARCHS=89" in dockerfile
    assert "torch==2.9.1" in dockerfile
    assert "flash-attn==2.8.3" in dockerfile
    assert "python3.12-venv" in dockerfile
    assert "VIRTUAL_ENV=/opt/venv" in dockerfile
    assert 'python3.12 -m venv "$VIRTUAL_ENV"' in dockerfile
    assert "--break-system-packages" not in dockerfile
    assert "assert sys.prefix == '/opt/venv'" in dockerfile
    assert "COPY src /opt/factory/src" in dockerfile
    assert "COPY . /opt/factory" not in dockerfile
    assert "ai_video_factory.workers.breeze_tts2.runtime:app" in entrypoint
