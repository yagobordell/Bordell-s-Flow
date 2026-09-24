import pytest
from PIL import Image

from ai_video_factory.workers.qwen_image_21.model import validate_qwen_output_image


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_qwen_output_accepts_opaque_image(mode: str) -> None:
    image = Image.new(mode, (32, 32), (90, 110, 140, 255) if mode == "RGBA" else (90, 110, 140))

    validate_qwen_output_image(image, width=32, height=32)

    assert image.mode == mode


def test_qwen_output_rejects_corrupt_nearly_transparent_rgba() -> None:
    image = Image.new("RGBA", (32, 32), (165, 57, 209, 1))
    image.paste((90, 150, 20, 255), (0, 0, 32, 1))

    with pytest.raises(RuntimeError, match="predominantly transparent"):
        validate_qwen_output_image(image, width=32, height=32)


def test_qwen_output_rejects_semitransparent_production_frame() -> None:
    image = Image.new("RGBA", (32, 32), (90, 110, 140, 128))

    with pytest.raises(RuntimeError, match="predominantly transparent"):
        validate_qwen_output_image(image, width=32, height=32)


def test_qwen_output_rejects_wrong_dimensions_or_mode() -> None:
    with pytest.raises(RuntimeError, match="dimensions"):
        validate_qwen_output_image(Image.new("RGB", (32, 32)), width=1280, height=736)

    with pytest.raises(RuntimeError, match="mode"):
        validate_qwen_output_image(Image.new("L", (32, 32)), width=32, height=32)
