import asyncio
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from ai_video_factory.domain import Shot, StoryboardFrame
from ai_video_factory.providers.images import GeneratedImage
from ai_video_factory.workflows.storyboard_keyframes import generate_storyboard_keyframes


def _png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


class ParallelImageProvider:
    def __init__(self, expected_calls: int) -> None:
        self.expected_calls = expected_calls
        self.started = 0
        self.all_started = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        self.calls.append(kwargs)
        self.started += 1
        if self.started == self.expected_calls:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        width, height = (int(part) for part in str(kwargs["size"]).split("x"))
        return GeneratedImage(
            content=_png_bytes(width, height),
            media_type="image/png",
            extension="png",
        )


def _frames() -> list[StoryboardFrame]:
    return [
        StoryboardFrame(shot_id=1, prompt='{"caption":"Frame one"}'),
        StoryboardFrame(shot_id=2, prompt='{"caption":"Frame two"}'),
    ]


def _shots() -> list[Shot]:
    return [
        Shot(
            id=1,
            scene_id=1,
            beat_ids=[1],
            entity_ids=["group_001"],
            action="Warriors rule.",
        ),
        Shot(
            id=2,
            scene_id=1,
            beat_ids=[2],
            entity_ids=["group_001", "location_001"],
            action="Warriors stand in the stronghold.",
        ),
    ]


def test_storyboard_keyframes_run_in_parallel_without_binary_reference_inputs(
    tmp_path: Any,
) -> None:
    output_dir = tmp_path / "phase6" / "storyboard_keyframes"
    provider = ParallelImageProvider(expected_calls=2)

    keyframes = asyncio.run(
        generate_storyboard_keyframes(
            _frames(),
            _shots(),
            image_provider=provider,  # type: ignore[arg-type]
            output_dir=output_dir,
            model="ideogram-ai/ideogram-4-nf4",
            size="1536x864",
            quality="high",
        )
    )

    assert provider.started == 2
    assert [keyframe.model_dump() for keyframe in keyframes] == [
        {"shot_id": 1, "uri": "storyboard_keyframes/shot_001.png"},
        {"shot_id": 2, "uri": "storyboard_keyframes/shot_002.png"},
    ]
    assert [call["prompt"] for call in provider.calls] == [
        '{"caption":"Frame one"}',
        '{"caption":"Frame two"}',
    ]
    assert all("references" not in call for call in provider.calls)
    with Image.open(output_dir / "shot_001.png") as image:
        assert image.size == (1536, 864)
    with Image.open(output_dir / "shot_002.png") as image:
        assert image.size == (1536, 864)


class WrongSizeStoryboardProvider:
    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        return GeneratedImage(
            content=_png_bytes(864, 1536),
            media_type="image/png",
            extension="png",
        )


def test_storyboard_keyframes_reject_non_landscape_provider_geometry(tmp_path: Any) -> None:
    output_dir = tmp_path / "phase6" / "storyboard_keyframes"

    with pytest.raises(ValueError, match="expected exactly 1536x864"):
        asyncio.run(
            generate_storyboard_keyframes(
                _frames(),
                _shots(),
                image_provider=WrongSizeStoryboardProvider(),  # type: ignore[arg-type]
                output_dir=output_dir,
                model="test",
                size="1536x864",
                quality="high",
            )
        )

    assert not output_dir.exists()


class FailingStoryboardProvider:
    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        if "fail" in kwargs["prompt"]:
            raise RuntimeError("keyframe generation failed")
        return GeneratedImage(content=b"unused", media_type="image/png", extension="png")


def test_storyboard_keyframe_failure_writes_no_partial_batch(tmp_path: Any) -> None:
    output_dir = tmp_path / "phase6" / "storyboard_keyframes"
    frames = [
        StoryboardFrame(shot_id=1, prompt='{"caption":"ok"}'),
        StoryboardFrame(shot_id=2, prompt='{"caption":"fail"}'),
    ]
    shots = [
        Shot(id=1, scene_id=1, beat_ids=[1], entity_ids=[], action="First"),
        Shot(id=2, scene_id=1, beat_ids=[2], entity_ids=[], action="Second"),
    ]

    with pytest.raises(RuntimeError, match="keyframe generation failed"):
        asyncio.run(
            generate_storyboard_keyframes(
                frames,
                shots,
                image_provider=FailingStoryboardProvider(),  # type: ignore[arg-type]
                output_dir=output_dir,
                model="ideogram-ai/ideogram-4-nf4",
                size="1536x864",
                quality="high",
            )
        )

    assert not output_dir.exists()
