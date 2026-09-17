from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.providers.salad_flux import build_flux_job_request
from ai_video_factory.workers.flux_schnell import (
    FLUX_SCHNELL_GENERATION_PROFILE,
    FLUX_SCHNELL_MODEL_ID,
    FLUX_SCHNELL_REFERENCE_TASK,
    FluxSchnellImageTaskRunner,
    FluxSchnellWorkerSettings,
)
from ai_video_factory.workers.flux_schnell.model import FluxImageParameters


def _caption() -> str:
    return (
        '{"high_level_description":"Canonical location reference. Empty rocky valley.",'
        '"style_description":{"aesthetics":"documentary realism",'
        '"lighting":"neutral daylight","photo":"realistic photography",'
        '"medium":"documentary photograph"},'
        '"compositional_deconstruction":{"background":"Distant mountain ridge.",'
        '"elements":[]}}'
    )


class FakeBackend:
    def __init__(self) -> None:
        self.prepared = 0
        self.ready_calls = 0
        self.parameters: FluxImageParameters | None = None

    def prepare(self) -> None:
        self.prepared += 1

    def ready(self) -> None:
        self.ready_calls += 1

    def generate(self, *, parameters: FluxImageParameters, output_path: Path) -> None:
        self.parameters = parameters
        output_path.write_bytes(b"png-output")


def test_flux_request_is_deterministic_and_uses_independent_namespace() -> None:
    first = build_flux_job_request(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        caption=_caption(),
        model_id=FLUX_SCHNELL_MODEL_ID,
        width=1024,
        height=1024,
    )
    second = build_flux_job_request(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        caption=_caption(),
        model_id=FLUX_SCHNELL_MODEL_ID,
        width=1024,
        height=1024,
    )

    assert first == second
    assert first.job_id.startswith("flux-reference-")
    assert first.output.key == f"jobs/{first.job_id}/image.png"
    assert first.parameters["generation_profile"] == FLUX_SCHNELL_GENERATION_PROFILE
    assert first.parameters["model_id"] == FLUX_SCHNELL_MODEL_ID
    assert first.parameters["seed"] == second.parameters["seed"]


def test_flux_runner_validates_request_and_persists_png(tmp_path: Path) -> None:
    request = build_flux_job_request(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        caption=_caption(),
        model_id=FLUX_SCHNELL_MODEL_ID,
        width=1024,
        height=1024,
    )
    backend = FakeBackend()
    runner = FluxSchnellImageTaskRunner(
        backend=backend,
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
    )

    artifact = runner.run(request, {}, tmp_path)

    assert artifact.path.read_bytes() == b"png-output"
    assert artifact.content_type == "image/png"
    assert backend.parameters is not None
    assert backend.parameters.prompt == request.parameters["prompt"]


def test_flux_runner_rejects_unexpected_task(tmp_path: Path) -> None:
    backend = FakeBackend()
    runner = FluxSchnellImageTaskRunner(
        backend=backend,
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
    )
    request = InferenceJobRequest(
        job_id="flux-reference-wrongtask",
        task="image.flux_schnell.other",
        output=ObjectOutput(
            key="jobs/flux-reference-wrongtask/image.png",
            content_type="image/png",
        ),
        parameters={
            "generation_profile": FLUX_SCHNELL_GENERATION_PROFILE,
            "model_id": FLUX_SCHNELL_MODEL_ID,
            "prompt": "landscape",
            "width": 1024,
            "height": 1024,
            "seed": 1,
        },
    )

    with pytest.raises(ValueError, match="cannot execute task"):
        runner.run(request, {}, tmp_path)


def test_flux_parameters_require_16_pixel_alignment() -> None:
    with pytest.raises(ValidationError, match="divisible by 16"):
        FluxImageParameters(
            generation_profile=FLUX_SCHNELL_GENERATION_PROFILE,
            model_id=FLUX_SCHNELL_MODEL_ID,
            prompt="landscape",
            width=1025,
            height=1024,
            seed=1,
        )


def test_flux_settings_pin_open_schnell_model_and_nf4() -> None:
    settings = FluxSchnellWorkerSettings(_env_file=None, worker_mode="local")

    assert settings.model_repository == FLUX_SCHNELL_MODEL_ID
    assert settings.quantization == "bnb4-nf4"
    assert settings.inference_steps == 4
