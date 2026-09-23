import json
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.breeze_tts2 import (
    BREEZE_TTS2_GENERATION_PROFILE,
    BREEZE_TTS2_MODEL_ID,
    BREEZE_TTS2_TASK,
    BreezeSpeechTaskRunner,
    BreezeTTS2Backend,
    BreezeTTS2WorkerSettings,
    breeze_application_job_id,
)
from ai_video_factory.workers.breeze_tts2.model import (
    _MAX_PROMPT_TOKENS,
    _atempo_chain,
    _BreezeBindings,
    _plan_narration_chunks,
    _split_narration_text,
)


class _RecordingFastConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _PreparedRuntime:
    fast_enabled = True
    codec_chunk_frames = 1

    def warmup_from_profile(self, profile: Any) -> None:
        self.profile = profile


@dataclass(frozen=True)
class _WarmupProfile:
    codec_chunk_frames: int = 1


def test_breeze_reference_runtime_uses_eager_prefill_and_fast_decode() -> None:
    config = _RecordingFastConfig()
    runtime = _PreparedRuntime()

    def build_config(**kwargs: Any) -> _RecordingFastConfig:
        config.kwargs = kwargs
        return config

    bindings = _BreezeBindings(
        torch=object(),
        np=object(),
        soundfile=object(),
        load_runtime=lambda *_args, **_kwargs: (object(), object(), object()),
        set_all_seeds=lambda *_args, **_kwargs: None,
        update_generation_config_for_breeze=lambda *_args, **_kwargs: None,
        get_template=object(),
        prepare_inputs=object(),
        select_template_name=object(),
        fast_runtime_type=lambda *_args, **_kwargs: runtime,
        fast_config_type=build_config,
        load_warmup_profile=lambda *_args, **_kwargs: _WarmupProfile(),
    )
    backend = BreezeTTS2Backend(
        model_root=Path("models"),
        runtime_root=Path("runtime"),
        device="cpu",
    )

    backend._bindings = bindings
    assert backend._get_or_build_runtime(bindings) is runtime

    assert config.kwargs["fast_all"] is None
    assert config.kwargs["fast_text_encoder"] is True
    assert config.kwargs["fast_backbone_prefill"] is False
    assert config.kwargs["fast_backbone_decode"] is True
    assert config.kwargs["fast_depth_decoder"] is True
    assert config.kwargs["fast_codec"] is True


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


def test_breeze_backend_waits_for_atomic_bootstrap_marker(tmp_path: Path) -> None:
    backend = BreezeTTS2Backend(
        model_root=tmp_path / "breeze",
        runtime_root=tmp_path / "runtime",
        model_repository=BREEZE_TTS2_MODEL_ID,
        model_revision="main",
        device="cpu",
    )

    with pytest.raises(ModelBootstrapPendingError, match="bootstrap marker is missing"):
        backend.prepare()


def test_breeze_backend_rejects_wrong_bootstrap_revision(tmp_path: Path) -> None:
    model_root = tmp_path / "breeze"
    model_root.mkdir()
    (model_root / ".ready").write_text("BreezeBlue/Breeze-TTS-2@old\n", encoding="utf-8")
    backend = BreezeTTS2Backend(
        model_root=model_root,
        runtime_root=tmp_path / "runtime",
        model_repository=BREEZE_TTS2_MODEL_ID,
        model_revision="main",
        device="cpu",
    )

    with pytest.raises(RuntimeError, match="does not match"):
        backend.prepare()


def test_breeze_text_chunking_preserves_content_order() -> None:
    text = "First sentence is short. Second sentence is also short. Third sentence ends here."
    chunks = _split_narration_text(text, 35)

    assert len(chunks) >= 2
    assert " ".join(chunks) == text
    assert all(len(chunk) <= 35 for chunk in chunks)


class _WordTokenizer:
    def __call__(self, text: str, *, add_special_tokens: bool):
        del add_special_tokens
        return {"input_ids": text.split()}


def test_breeze_prefers_one_request_when_prompt_fits_runtime_budget() -> None:
    text = " ".join(f"word{index}" for index in range(400))

    chunks = _plan_narration_chunks(
        text,
        tokenizer=_WordTokenizer(),
        instruction="Speak naturally.",
        max_chunk_chars=4000,
    )

    assert chunks == [text]


def test_breeze_output_guard_splits_two_minute_script_without_voice_reset() -> None:
    text = Path("examples/input/script_2min_english.txt").read_text(encoding="utf-8").strip()

    chunks = _plan_narration_chunks(
        text,
        tokenizer=_WordTokenizer(),
        instruction="Speak naturally.",
        max_chunk_chars=1200,
    )

    assert len(chunks) == 2
    assert " ".join(chunks) == " ".join(text.split())
    assert chunks[0].endswith("the same sky.")
    assert chunks[1].startswith("During the afternoon,")
    assert all(len(chunk) <= 1200 for chunk in chunks)


def test_breeze_anchors_context_overflow_chunks() -> None:
    text = "First sentence establishes the voice. " + " ".join(
        f"word{index}" for index in range(_MAX_PROMPT_TOKENS * 2)
    )

    chunks = _plan_narration_chunks(
        text,
        tokenizer=_WordTokenizer(),
        instruction="Speak naturally.",
        max_chunk_chars=4000,
    )

    assert len(chunks) >= 3
    assert chunks[0] == "First sentence establishes the voice."
    assert " ".join(chunks) == text


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
    assert service["resources"]["gpu_class_names"] == ["RTX 4090 (24 GB)"]
    assert service["dockerfile"] == "docker/workers/breeze-tts2/Dockerfile"
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 2


def test_breeze_container_pins_runtime_and_targets_4090() -> None:
    dockerfile = Path("docker/workers/breeze-tts2/Dockerfile").read_text(encoding="utf-8")
    entrypoint = Path("docker/workers/breeze-tts2/entrypoint.sh").read_text(encoding="utf-8")
    downloader = Path("docker/workers/breeze-tts2/download_models.sh").read_text(
        encoding="utf-8"
    )
    runtime = Path("src/ai_video_factory/workers/breeze_tts2/runtime.py").read_text(
        encoding="utf-8"
    )

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
    assert 'staging_root="${model_root}.staging"' in downloader
    assert 'printf \'%s\\n\' "${expected_marker}" > "${staging_root}/.ready"' in downloader
    assert 'mv "${staging_root}" "${model_root}"' in downloader
    assert "model_repository=settings.model_repository" in runtime
    assert "model_revision=settings.model_revision" in runtime
