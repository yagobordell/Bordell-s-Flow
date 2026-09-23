from pathlib import Path

import pytest

from ai_video_factory.providers.salad_qwen_image import build_qwen_image_job_request
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_GENERATION_PROFILE,
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_MODEL_REVISION,
    QWEN_IMAGE_21_REFERENCE_TASK,
)


def test_qwen_reference_request_is_deterministic() -> None:
    first = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_REFERENCE_TASK,
        prompt="cinematic mountain valley",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1536,
        height=864,
    )
    second = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_REFERENCE_TASK,
        prompt="cinematic mountain valley",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1536,
        height=864,
    )

    assert first == second
    assert first.job_id.startswith("qwen-image-21-reference-")
    assert first.parameters["generation_profile"] == QWEN_IMAGE_21_GENERATION_PROFILE
    assert first.parameters["model_revision"] == QWEN_IMAGE_21_MODEL_REVISION
    assert first.parameters["num_inference_steps"] == 40
    assert first.output.content_type == "image/png"


def test_qwen_keyframe_request_uses_distinct_task_namespace() -> None:
    request = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
        prompt="wide establishing shot",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1536,
        height=864,
    )

    assert request.job_id.startswith("qwen-image-21-keyframe-")
    assert request.task == QWEN_IMAGE_21_KEYFRAME_TASK


@pytest.mark.parametrize("width,height", [(1537, 864), (1536, 865), (128, 128)])
def test_qwen_request_rejects_invalid_dimensions(width: int, height: int) -> None:
    from ai_video_factory.providers.salad_qwen_image import _parse_size

    with pytest.raises(ValueError):
        _parse_size(f"{width}x{height}")


def test_qwen_salad_manifest_contract() -> None:
    import json

    manifest = json.loads(Path("deploy/salad/services.json").read_text(encoding="utf-8"))
    service = manifest["services"]["qwen_image_21"]

    assert service["priority"] == "high"
    assert service["resources"]["gpu_class_names"] == ["RTX 5090 (32 GB)"]
    assert service["environment"]["QWEN_IMAGE_21_MODEL_REPOSITORY"] == QWEN_IMAGE_21_MODEL_ID
    assert service["environment"]["QWEN_IMAGE_21_MODEL_REVISION"] == QWEN_IMAGE_21_MODEL_REVISION


def test_salad_smoke_suite_uses_qwen_image_21() -> None:
    script = Path("scripts/run_salad_smoke_suite.py").read_text(encoding="utf-8")

    assert '"qwen_image_21"' in script
    assert "SaladQwenImage21Provider" in script
    assert "QWEN_IMAGE_21_KEYFRAME_TASK" in script
    assert "qwen-image-21-keyframe.png" in script
    assert "ai-video-factory-ltx25-jobs-v2" in script
    assert "ideogram" not in script.lower()
