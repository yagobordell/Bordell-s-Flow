import json
from pathlib import Path

import pytest

from ai_video_factory.gpu.ltx_jobs import ltx_video_application_job_id as legacy_job_id
from ai_video_factory.gpu.ltx_video import DirectLTX25Backend as LegacyBackend
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.ltx25 import (
    LTX_VIDEO_TASK,
    DirectLTX25Backend,
    LTX25WorkerSettings,
    ltx_video_application_job_id,
)


def test_legacy_ltx_imports_resolve_to_dedicated_worker() -> None:
    assert issubclass(LegacyBackend, DirectLTX25Backend)
    assert legacy_job_id is ltx_video_application_job_id
    assert LTX_VIDEO_TASK == "video.ltx25.generate"


def test_ltx25_prepare_treats_missing_model_files_as_bootstrap_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DirectLTX25Backend(model_root=tmp_path)
    monkeypatch.setattr(backend, "_get_bindings", lambda: object())

    with pytest.raises(ModelBootstrapPendingError, match="Missing LTX-2.5 model files"):
        backend.prepare()


def test_ltx25_worker_settings_use_shared_inference_core() -> None:
    settings = LTX25WorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_lease_seconds=60,
        worker_heartbeat_seconds=10,
    )

    assert settings.worker_mode == "local"
    assert settings.model_root.as_posix() == "/workspace/models/ltx-2.5"
    assert settings.model_repository == "Lightricks/LTX-2.5"
    assert settings.device == "cuda"


def test_ltx25_salad_manifest_has_dedicated_queue_and_image() -> None:
    manifest_path = Path("deploy/salad/services.json")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    service = document["services"]["ltx25"]

    assert service["queue_name"] == "ai-video-factory-ltx25-jobs-v2"
    assert service["dockerfile"] == "docker/workers/ltx25/Dockerfile"
    assert "ltx25" in service["image"]
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert "gpu_classes" not in service["resources"]
    assert service["autoscaler"]["min_replicas"] == 0
    assert service["autoscaler"]["max_replicas"] == 4
    assert service["autoscaler"]["max_upscale_per_minute"] == 2
    assert service["environment"]["INFERENCE_WORKER_MODE"] == "production"
    assert "GPU_WORKER_RUNTIME" not in service["environment"]


def test_ltx25_container_is_model_specific() -> None:
    dockerfile = Path("docker/workers/ltx25/Dockerfile")
    text = dockerfile.read_text(encoding="utf-8")

    assert dockerfile.is_file()
    assert not Path("docker/phase8-worker/Dockerfile").exists()
    assert "ai_video_factory.workers.ltx25.runtime:app" in Path(
        "docker/workers/ltx25/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "COPY src /opt/factory/src" in text
    assert "COPY . /opt/factory" not in text


def test_ltx25_model_download_uses_xet_with_resilient_timeouts() -> None:
    dockerfile = Path("docker/workers/ltx25/Dockerfile").read_text(encoding="utf-8")
    bootstrap = Path("docker/workers/ltx25/download_models.sh").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        Path("deploy/salad/services.json").read_text(encoding="utf-8")
    )
    service = manifest["services"]["ltx25"]

    assert "HF_HUB_DOWNLOAD_TIMEOUT=120" in dockerfile
    assert "HF_HUB_ETAG_TIMEOUT=30" in dockerfile
    assert "HF_HUB_DISABLE_XET" not in dockerfile
    assert "HF_HUB_ENABLE_HF_TRANSFER" not in dockerfile
    assert "'huggingface-hub==1.31.0'" in dockerfile
    assert "'hf-xet==1.6.0'" in dockerfile

    assert "HF_DOWNLOAD_RUNTIME" in bootstrap
    assert "huggingface_hub=" in bootstrap
    assert "hf_xet=" in bootstrap
    assert "xet_disabled=" in bootstrap

    assert service["image"].endswith(
        ":ltx25-torch211-cu128-eagersdpa-xet-v4"
    )
