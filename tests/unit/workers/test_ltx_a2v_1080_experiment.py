"""Experimental A2V 1080p request geometry: padding must not stretch/crop the delivered frame."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.workers.ltx25.a2v import (
    LTXAudioToVideoParameters,
    _prepare_avatar_image,
)
from ai_video_factory.workers.ltx25.model import (
    _crop_video_chunk_to_requested,
    _pipeline_dimensions,
)


def test_a2v_accepts_1080_output_and_pads_only_internal_grid() -> None:
    request = LTXAudioToVideoParameters(width=1920, height=1080, fps=24)
    assert _pipeline_dimensions(request) == (1920, 1088)
    assert (request.width, request.height) == (1920, 1080)
    assert _pipeline_dimensions(LTXAudioToVideoParameters()) == (1920, 1088)
    assert _pipeline_dimensions(
        LTXAudioToVideoParameters(width=1280, height=720)
    ) == (1280, 768)
    with pytest.raises(ValidationError, match="divisible by 64"):
        LTXAudioToVideoParameters(width=1920, height=1081)


def test_a2v_1080_avatar_grid_padding_keeps_full_requested_image(tmp_path: Path) -> None:
    source = tmp_path / "avatar.png"
    image = Image.new("RGB", (1920, 1080), (10, 20, 30))
    image.paste((240, 30, 10), (0, 0, 1920, 1))
    image.paste((20, 230, 20), (0, 1079, 1920, 1080))
    image.save(source)
    prepared = _prepare_avatar_image(
        source,
        tmp_path / "padded.png",
        requested_width=1920,
        requested_height=1080,
        pipeline_width=1920,
        pipeline_height=1088,
    )
    with Image.open(prepared) as padded:
        assert padded.size == (1920, 1088)
        assert padded.getpixel((900, 0)) == image.getpixel((900, 0))
        assert padded.getpixel((900, 3)) == image.getpixel((900, 0))
        assert padded.getpixel((900, 4)) == image.getpixel((900, 0))
        assert padded.getpixel((900, 1083)) == image.getpixel((900, 1079))
        assert padded.getpixel((900, 1087)) == image.getpixel((900, 1079))


def test_a2v_1080_decode_crop_removes_only_grid_padding() -> None:
    class RecordingDecodedChunk:
        shape = (2, 1088, 1920, 3)

        def __getitem__(self, key: tuple[object, ...]) -> tuple[object, ...]:
            return key

    slices = _crop_video_chunk_to_requested(
        RecordingDecodedChunk(), requested_width=1920, requested_height=1080
    )
    assert slices[0] is Ellipsis
    assert slices[1] == slice(4, 1084)
    assert slices[2] == slice(0, 1920)
    assert slices[3] == slice(None)
