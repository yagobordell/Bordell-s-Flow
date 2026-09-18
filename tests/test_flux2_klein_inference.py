from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from ai_video_factory.workers.flux2_klein.model import (
    FLUX2_KLEIN_GENERATION_PROFILE,
    FLUX2_KLEIN_MODEL_ID,
    FLUX2_KLEIN_MODEL_REVISION,
    Flux2KleinBackend,
    Flux2KleinImageParameters,
)


class _FakeGenerator:
    def __init__(self, *, device: str) -> None:
        self.device = device
        self.seed: int | None = None

    def manual_seed(self, seed: int):
        self.seed = seed
        return self


class _FakeCuda:
    @staticmethod
    def is_available() -> bool:
        return False

    @staticmethod
    def empty_cache() -> None:
        return None


class _FakeTorch:
    cuda = _FakeCuda()

    @staticmethod
    def Generator(*, device: str) -> _FakeGenerator:
        return _FakeGenerator(device=device)


class _FakePipeline:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            images=[Image.new("RGB", (int(kwargs["width"]), int(kwargs["height"])))]
        )


def test_flux2_klein_request_to_inference_writes_valid_png_with_seed(tmp_path: Path) -> None:
    backend = Flux2KleinBackend(
        model_root=tmp_path / "model",
        model_repository=FLUX2_KLEIN_MODEL_ID,
        model_revision=FLUX2_KLEIN_MODEL_REVISION,
        bootstrap_status_path=tmp_path / "bootstrap.json",
        device="cpu",
    )
    pipeline = _FakePipeline()
    backend._pipeline = pipeline
    backend._torch = _FakeTorch()
    parameters = Flux2KleinImageParameters(
        generation_profile=FLUX2_KLEIN_GENERATION_PROFILE,
        model_id=FLUX2_KLEIN_MODEL_ID,
        model_revision=FLUX2_KLEIN_MODEL_REVISION,
        prompt="A documentary photograph of a mountain observatory at blue hour.",
        width=1024,
        height=1024,
        seed=12345,
        num_inference_steps=4,
        guidance_scale=1.0,
        max_sequence_length=512,
    )
    output = tmp_path / "image.png"

    backend.generate(parameters=parameters, output_path=output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(output) as image:
        assert image.size == (1024, 1024)
        assert image.format == "PNG"
    call = pipeline.calls[0]
    assert call["num_inference_steps"] == 4
    assert call["guidance_scale"] == 1.0
    assert call["max_sequence_length"] == 512
    generator = call["generator"]
    assert isinstance(generator, _FakeGenerator)
    assert generator.device == "cpu"
    assert generator.seed == 12345
