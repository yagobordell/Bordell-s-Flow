from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.salad_qwen_image import build_qwen_image_job_request
from ai_video_factory.workers.qwen_image_21 import (
    QWEN_IMAGE_21_GENERATION_PROFILE,
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_MODEL_ID,
    QWEN_IMAGE_21_MODEL_REVISION,
)
from ai_video_factory.workers.qwen_image_21.model import (
    QWEN_IMAGE_21_BENCHMARK_PROFILE,
    QwenImage21ImageTaskRunner,
    QwenImage21Parameters,
    _validate_int8_cuda_device_map,
)
from scripts.smoke.submit_qwen_image_21_benchmark import _make_request, _validate_metrics


def _parameters(profile: str, width: int, height: int) -> dict[str, object]:
    return {
        "generation_profile": profile,
        "model_id": QWEN_IMAGE_21_MODEL_ID,
        "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
        "prompt": "A monk speaking.",
        "width": width,
        "height": height,
        "seed": 4242,
    }


def test_benchmark_profile_does_not_change_the_production_size() -> None:
    with pytest.raises(ValueError, match="production generation is fixed"):
        QwenImage21Parameters.model_validate(
            _parameters(QWEN_IMAGE_21_GENERATION_PROFILE, 1536, 864)
        )
    with pytest.raises(ValueError, match="benchmark requires 1536x864"):
        QwenImage21Parameters.model_validate(
            _parameters(QWEN_IMAGE_21_BENCHMARK_PROFILE, 1280, 736)
        )
    validated = QwenImage21Parameters.model_validate(
        _parameters(QWEN_IMAGE_21_BENCHMARK_PROFILE, 1536, 864)
    )
    assert validated.num_inference_steps == 40
    assert validated.use_kv_cache is True
    assert validated.true_cfg_scale == 1.0


def test_benchmark_requests_keep_exact_prompt_and_seed_but_never_replay() -> None:
    first = _make_request(job_id="qwen-benchmark-a-01", prompt="Original prompt.", seed=4242)
    second = _make_request(job_id="qwen-benchmark-b-02", prompt="Original prompt.", seed=4242)
    assert first.parameters == second.parameters
    assert first.fingerprint() != second.fingerprint()
    assert first.output.key != second.output.key
    assert first.max_attempts == second.max_attempts == 1
    assert first.sidecar_outputs is not None
    assert set(first.sidecar_outputs) == {"metadata"}
    assert first.parameters["generation_profile"] == QWEN_IMAGE_21_BENCHMARK_PROFILE
    assert first.parameters["width"] == 1536
    assert first.parameters["height"] == 864

    production = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
        prompt="Original prompt.",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1280,
        height=736,
    )
    assert production.sidecar_outputs is None
    assert production.parameters["generation_profile"] == QWEN_IMAGE_21_GENERATION_PROFILE


class _FakeBackend:
    def generate(self, *, parameters: QwenImage21Parameters, output_path: Path) -> dict:
        Image.new("RGB", (parameters.width, parameters.height)).save(output_path, format="PNG")
        return {"inference_seconds": 1.0, "worker_id": "worker-test"}


def test_qwen_benchmark_metadata_sidecar_is_optional_for_production(tmp_path: Path) -> None:
    runner = QwenImage21ImageTaskRunner(
        backend=_FakeBackend(), task_name=QWEN_IMAGE_21_KEYFRAME_TASK
    )
    benchmark = _make_request(
        job_id="qwen-benchmark-a-03", prompt="Original prompt.", seed=4242
    )
    artifact = runner.run(benchmark, {}, tmp_path)
    assert artifact.path.is_file()
    assert len(artifact.sidecars) == 1
    sidecar = artifact.sidecars[0]
    assert sidecar.name == "metadata"
    assert sidecar.content_type == "application/json"
    assert json.loads(sidecar.path.read_text(encoding="utf-8")) == {
        "inference_seconds": 1.0,
        "worker_id": "worker-test",
    }

    production = build_qwen_image_job_request(
        task_name=QWEN_IMAGE_21_KEYFRAME_TASK,
        prompt="Original prompt.",
        model_id=QWEN_IMAGE_21_MODEL_ID,
        width=1280,
        height=736,
    )
    result = runner.run(production, {}, tmp_path)
    assert result.sidecars == ()


def test_qwen_rejects_unexpected_sidecars_before_inference(tmp_path: Path) -> None:
    runner = QwenImage21ImageTaskRunner(
        backend=_FakeBackend(), task_name=QWEN_IMAGE_21_KEYFRAME_TASK
    )
    request = _make_request(job_id="qwen-benchmark-a-04", prompt="P", seed=4242)
    invalid = InferenceJobRequest(
        job_id=request.job_id,
        task=request.task,
        output=request.output,
        parameters=request.parameters,
        sidecar_outputs={
            "unexpected": ObjectOutput(
                key=f"jobs/{request.job_id}/unknown.json", content_type="application/json"
            )
        },
    )
    with pytest.raises(ValueError, match="one JSON metadata sidecar"):
        runner.run(invalid, {}, tmp_path)


def test_qwen_rejects_a_restarted_worker_or_pipeline() -> None:
    metrics = {
        "generation_profile": QWEN_IMAGE_21_BENCHMARK_PROFILE,
        "model_revision": QWEN_IMAGE_21_MODEL_REVISION,
        "width": 1536,
        "height": 864,
        "num_inference_steps": 40,
        "seed": 4242,
        "memory_mode": "int8_cuda",
        "worker_id": "same-worker",
        "pipeline_reused": True,
        "inference_seconds": 29.0,
        "png_save_seconds": 0.3,
        "total_elapsed_seconds": 29.4,
        "peak_vram_allocated_bytes": 1024,
    }
    assert _validate_metrics(metrics, seed=4242, worker_id=None, generation=1) == "same-worker"
    assert _validate_metrics(metrics, seed=4242, worker_id="same-worker", generation=2)
    with pytest.raises(RuntimeError, match="worker process changed"):
        _validate_metrics(metrics, seed=4242, worker_id="other-worker", generation=2)
    with pytest.raises(RuntimeError, match="rebuilt"):
        _validate_metrics(
            {**metrics, "pipeline_reused": False},
            seed=4242,
            worker_id="same-worker",
            generation=2,
        )


@pytest.mark.parametrize(
    "device_map",
    ["cuda", "cuda:0", 0, {"transformer": "cuda", "text_encoder": 0, "vae": "cuda:0"}],
)
def test_qwen_int8_cuda_placement_accepts_single_device_or_map(device_map: object) -> None:
    if isinstance(device_map, int):
        device_map = {"transformer": device_map}
    _validate_int8_cuda_device_map(device_map)


@pytest.mark.parametrize(
    "device_map",
    [None, "", {}, "cpu", "disk", "balanced", {"transformer": "cuda", "vae": "cpu"}],
)
def test_qwen_int8_cuda_placement_rejects_missing_or_offloaded_map(device_map: object) -> None:
    with pytest.raises(RuntimeError, match="device map|not fully on CUDA"):
        _validate_int8_cuda_device_map(device_map)
