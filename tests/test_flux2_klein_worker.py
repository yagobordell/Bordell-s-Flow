from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.flux2_klein.model import (
    FLUX2_KLEIN_GENERATION_PROFILE,
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_MODEL_REVISION,
    FLUX2_KLEIN_REFERENCE_TASK,
    Flux2KleinBackend,
    Flux2KleinImageTaskRunner,
)


class _FakeBackend:
    def prepare(self) -> None:
        return None

    def ready(self) -> None:
        return None

    def generate(self, *, parameters, output_path: Path) -> None:
        Image.new("RGB", (parameters.width, parameters.height), "white").save(
            output_path,
            format="PNG",
        )


def _request(seed: int = 1234) -> InferenceJobRequest:
    return InferenceJobRequest(
        job_id="flux2-klein-worker-test",
        task=FLUX2_KLEIN_REFERENCE_TASK,
        output=ObjectOutput(key="jobs/test/image.png", content_type="image/png"),
        parameters={
            "generation_profile": FLUX2_KLEIN_GENERATION_PROFILE,
            "model_id": FLUX2_KLEIN_MODEL_ID,
            "prompt": "A neutral documentary landscape",
            "width": 512,
            "height": 512,
            "seed": seed,
            "num_inference_steps": 4,
            "guidance_scale": 1.0,
        },
    )


def test_readiness_requires_exact_pinned_bootstrap_marker(tmp_path: Path) -> None:
    backend = Flux2KleinBackend(model_root=tmp_path)

    with pytest.raises(ModelBootstrapPendingError):
        backend.ready()

    (tmp_path / "snapshot").mkdir()
    (tmp_path / ".ready").write_text(
        f"{FLUX2_KLEIN_MODEL_ID}@wrong-revision\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelBootstrapPendingError):
        backend.ready()

    (tmp_path / ".ready").write_text(
        f"{FLUX2_KLEIN_MODEL_ID}@{FLUX2_KLEIN_MODEL_REVISION}\n",
        encoding="utf-8",
    )
    backend._pipeline = object()
    backend.ready()


def test_runner_writes_valid_png_with_provider_neutral_contract(tmp_path: Path) -> None:
    runner = Flux2KleinImageTaskRunner(
        backend=_FakeBackend(),  # type: ignore[arg-type]
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )
    artifact = runner.run(_request(), {}, tmp_path)

    assert artifact.content_type == "image/png"
    assert artifact.path.name == "image.png"
    with Image.open(artifact.path) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)


def test_seed_is_part_of_validated_inference_parameters(tmp_path: Path) -> None:
    seen: list[int] = []

    class RecordingBackend(_FakeBackend):
        def generate(self, *, parameters, output_path: Path) -> None:
            seen.append(parameters.seed)
            super().generate(parameters=parameters, output_path=output_path)

    runner = Flux2KleinImageTaskRunner(
        backend=RecordingBackend(),  # type: ignore[arg-type]
        task_name=FLUX2_KLEIN_REFERENCE_TASK,
    )
    runner.run(_request(seed=77), {}, tmp_path)
    assert seen == [77]
