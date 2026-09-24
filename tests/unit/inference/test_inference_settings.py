from __future__ import annotations

from ai_video_factory.inference.settings import InferenceWorkerSettings


def test_worker_id_appends_salad_instance_id(monkeypatch) -> None:
    monkeypatch.setenv("SALAD_INSTANCE_ID", "instance-123")

    settings = InferenceWorkerSettings(worker_mode="local", worker_id="worker-a")

    assert settings.worker_id == "worker-a-instance-123"


def test_worker_id_keeps_local_identity_without_salad_instance(monkeypatch) -> None:
    monkeypatch.delenv("SALAD_INSTANCE_ID", raising=False)

    settings = InferenceWorkerSettings(worker_mode="local", worker_id="worker-a")

    assert settings.worker_id == "worker-a"


def test_gpu_retry_cooldown_defaults_to_media_pipeline_value(monkeypatch) -> None:
    monkeypatch.delenv("INFERENCE_WORKER_GPU_RETRY_COOLDOWN_SECONDS", raising=False)

    settings = InferenceWorkerSettings(worker_mode="local", worker_id="worker-a")

    assert settings.worker_gpu_retry_cooldown_seconds == 60.0
