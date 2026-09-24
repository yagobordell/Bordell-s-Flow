from pathlib import Path

import pytest

from ai_video_factory.providers.salad_qwen_image import build_qwen_image_job_request
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_DEFAULT_STEPS,
    QWEN_IMAGE_21_GENERATION_PROFILE,
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_MODEL_REVISION,
    QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    QWEN_IMAGE_21_PRODUCTION_SIZE,
    QWEN_IMAGE_21_PRODUCTION_WIDTH,
    QWEN_IMAGE_21_REFERENCE_TASK,
    QWEN_IMAGE_21_TRUE_CFG_SCALE,
    QWEN_IMAGE_21_USE_KV_CACHE,
)


def test_qwen_reference_request_is_deterministic() -> None:
    first = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_REFERENCE_TASK,
        prompt="cinematic mountain valley",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=QWEN_IMAGE_21_PRODUCTION_WIDTH,
        height=QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    )
    second = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_REFERENCE_TASK,
        prompt="cinematic mountain valley",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=QWEN_IMAGE_21_PRODUCTION_WIDTH,
        height=QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    )

    assert first == second
    assert first.job_id.startswith("qwen-image-21-reference-")
    assert first.parameters["generation_profile"] == QWEN_IMAGE_21_GENERATION_PROFILE
    assert first.parameters["model_revision"] == QWEN_IMAGE_21_MODEL_REVISION
    assert first.parameters["num_inference_steps"] == QWEN_IMAGE_21_DEFAULT_STEPS
    assert first.parameters["true_cfg_scale"] == QWEN_IMAGE_21_TRUE_CFG_SCALE
    assert first.parameters["use_kv_cache"] is QWEN_IMAGE_21_USE_KV_CACHE
    assert first.output.content_type == "image/png"


def test_qwen_keyframe_request_uses_distinct_task_namespace() -> None:
    request = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
        prompt="wide establishing shot",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=QWEN_IMAGE_21_PRODUCTION_WIDTH,
        height=QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    )

    assert request.job_id.startswith("qwen-image-21-keyframe-")
    assert request.task == QWEN_IMAGE_21_KEYFRAME_TASK


@pytest.mark.parametrize(
    "width,height",
    [
        (1280, 720),
        (1536, 864),
        (1024, 576),
        (1536, 832),
        (1536, 896),
        (2752, 1536),
    ],
)
def test_qwen_request_rejects_non_production_dimensions(width: int, height: int) -> None:
    from ai_video_factory.providers.salad_qwen_image import _parse_size

    with pytest.raises(ValueError):
        _parse_size(f"{width}x{height}")


def test_qwen_production_size_uses_native_grid_without_post_crop() -> None:
    from ai_video_factory.providers.salad_qwen_image import _parse_size
    from ai_video_factory.workers.qwen_image_21 import QWEN_IMAGE_21_DIMENSION_MULTIPLE

    width, height = _parse_size(QWEN_IMAGE_21_PRODUCTION_SIZE)

    assert (width, height) == (
        QWEN_IMAGE_21_PRODUCTION_WIDTH,
        QWEN_IMAGE_21_PRODUCTION_HEIGHT,
    )
    assert (width, height) == (1280, 736)
    assert width % QWEN_IMAGE_21_DIMENSION_MULTIPLE == 0
    assert height % QWEN_IMAGE_21_DIMENSION_MULTIPLE == 0


def test_qwen_salad_manifest_contract() -> None:
    import json

    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = manifest["services"]["qwen_image_21"]

    assert service["priority"] == "high"
    assert service["image"].endswith("qwen-image-2.1-int8-1280x736-v5")
    assert service["environment"]["QWEN_IMAGE_21_MEMORY_MODE"] == "int8_cuda"
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert service["environment"]["QWEN_IMAGE_21_MODEL_REPOSITORY"] == QWEN_IMAGE_21_MODEL_ID
    assert service["environment"]["QWEN_IMAGE_21_MODEL_REVISION"] == QWEN_IMAGE_21_MODEL_REVISION
    assert service["environment"]["QWEN_IMAGE_21_DOWNLOAD_STALL_TIMEOUT_SECONDS"] == "720"
    assert service["environment"]["QWEN_IMAGE_21_DOWNLOAD_HARD_TIMEOUT_SECONDS"] == "7200"
    assert service["environment"]["QWEN_IMAGE_21_DOWNLOAD_POLL_SECONDS"] == "15"
    assert (
        service["environment"]["QWEN_IMAGE_21_DOWNLOAD_MIN_PROGRESS_RESET_BYTES"]
        == "67108864"
    )
    assert service["environment"]["QWEN_IMAGE_21_DOWNLOAD_MIN_THROUGHPUT_MIBPS"] == "8"
    assert service["environment"]["SALAD_NETWORK_MIN_DOWNLOAD_MBPS"] == "100"
    assert service["environment"]["SALAD_NETWORK_TEST_ATTEMPTS"] == "3"
    assert "huggingface.co/Qwen/Qwen-Image-2.1/resolve/" in service["environment"][
        "SALAD_NETWORK_TEST_URL"
    ]
    assert service["environment"]["HF_HUB_DOWNLOAD_TIMEOUT"] == "60"
    assert service["environment"]["HF_HUB_ETAG_TIMEOUT"] == "15"
    assert service["environment"]["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] == "true"
    assert "HF_XET_HIGH_PERFORMANCE" not in service["environment"]


def test_salad_smoke_suite_uses_qwen_image_21() -> None:
    script = Path("scripts/smoke/run_salad_smoke_suite.py").read_text(encoding="utf-8")

    assert '"qwen_image_21"' in script
    assert "SaladQwenImage21Provider" in script
    assert "QWEN_IMAGE_21_KEYFRAME_TASK" in script
    assert "qwen-image-21-keyframe.png" in script
    assert "time.time_ns()" in script
    assert 'image.metadata.get("replayed") != "false"' in script
    assert "PostgresJobQueueClient" in script
    assert '"POSTGRES_DSN"' in script
    assert "SaladJobQueueClient" not in script
    assert "ideogram" not in script.lower()


def test_qwen_worker_pins_qwen_compatible_diffusers_revision() -> None:
    dockerfile = Path("docker/workers/qwen-image-2.1/Dockerfile").read_text(encoding="utf-8")
    pinned = (
        "git+https://github.com/huggingface/diffusers.git@"
        "0121a91f9d419ff7234c8a5923f82c244e6f1914"
    )

    assert pinned in dockerfile
    assert "'git+https://github.com/huggingface/diffusers.git'" not in dockerfile
    assert "'transformers==5.17.0'" in dockerfile
    assert "ca-certificates curl git python3-pip" in dockerfile
    assert "HF_XET_HIGH_PERFORMANCE=1" not in dockerfile
    assert "network_preflight.sh /usr/local/bin/network-preflight" in dockerfile

def test_qwen_bootstrap_validates_required_snapshot_files() -> None:
    script = Path("docker/workers/qwen-image-2.1/download_models.sh").read_text(
        encoding="utf-8"
    )

    for relative in (
        "model_index.json",
        "processor/tokenizer.json",
        "scheduler/scheduler_config.json",
        "text_encoder/model.safetensors.index.json",
        "transformer/diffusion_pytorch_model.safetensors.index.json",
        "vae/diffusion_pytorch_model.safetensors",
    ):
        assert f'"{relative}"' in script
    assert "snapshot_ready" in script
    assert "ai_video_factory.workers.download_watchdog" in script
    assert "--stall-timeout-seconds" in script
    assert "--hard-timeout-seconds" in script
    assert "--reallocate-on-slow" in script
    assert "--min-progress-reset-bytes" in script
    assert "QWEN_IMAGE_21_DOWNLOAD_MIN_PROGRESS_RESET_BYTES" in script
    assert "--min-throughput-mibps" in script
    assert "QWEN_IMAGE_21_DOWNLOAD_MIN_THROUGHPUT_MIBPS" in script
    assert "/usr/local/bin/network-preflight" in script
    assert 'download_args+=(--token "${HF_TOKEN}")' not in script

