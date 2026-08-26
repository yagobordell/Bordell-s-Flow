import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.domain import ReferenceAsset, Shot, StoryboardFrame
from ai_video_factory.providers.images import GeneratedImage, ImageReferenceInput
from ai_video_factory.providers.openai_images import OpenAIImageProvider
from ai_video_factory.workflows.storyboard_keyframes import generate_storyboard_keyframes


class FakeImagesResource:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.last_edit: dict[str, Any] | None = None
        self.last_generate: dict[str, Any] | None = None

    async def edit(self, **kwargs: Any) -> Any:
        self.last_edit = kwargs
        encoded = base64.b64encode(self.payload).decode("ascii")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

    async def generate(self, **kwargs: Any) -> Any:
        self.last_generate = kwargs
        encoded = base64.b64encode(self.payload).decode("ascii")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])


class FakeOpenAIClient:
    def __init__(self, payload: bytes) -> None:
        self.images = FakeImagesResource(payload)


def test_openai_image_provider_edits_with_reference_images_and_fidelity() -> None:
    client = FakeOpenAIClient(b"edited-png")
    provider = OpenAIImageProvider(client=client)  # type: ignore[arg-type]

    image = asyncio.run(
        provider.generate_image_with_references(
            prompt="New storyboard composition",
            references=[
                ImageReferenceInput(content=b"group", media_type="image/png"),
                ImageReferenceInput(content=b"place", media_type="image/webp"),
            ],
            model="gpt-image-2",
            size="1024x1536",
            quality="medium",
            output_format="png",
            input_fidelity="high",
        )
    )

    assert image.content == b"edited-png"
    assert image.media_type == "image/png"
    assert image.extension == "png"
    assert client.images.last_edit == {
        "image": [
            ("reference_001.png", b"group", "image/png"),
            ("reference_002.webp", b"place", "image/webp"),
        ],
        "model": "gpt-image-2",
        "prompt": "New storyboard composition",
        "n": 1,
        "size": "1024x1536",
        "quality": "medium",
        "output_format": "png",
        "input_fidelity": "high",
    }


class ParallelReferenceProvider:
    def __init__(self, expected_calls: int) -> None:
        self.expected_calls = expected_calls
        self.started = 0
        self.all_started = asyncio.Event()
        self.reference_calls: list[dict[str, Any]] = []
        self.generate_calls: list[dict[str, Any]] = []

    async def generate_image_with_references(self, **kwargs: Any) -> GeneratedImage:
        self.reference_calls.append(kwargs)
        return await self._finish(kwargs)

    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        self.generate_calls.append(kwargs)
        return await self._finish(kwargs)

    async def _finish(self, kwargs: dict[str, Any]) -> GeneratedImage:
        self.started += 1
        if self.started == self.expected_calls:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        return GeneratedImage(
            content=str(kwargs["prompt"]).encode("utf-8"),
            media_type="image/png",
            extension="png",
        )


def _frames() -> list[StoryboardFrame]:
    return [
        StoryboardFrame(shot_id=1, prompt="Frame one"),
        StoryboardFrame(shot_id=2, prompt="Frame two"),
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


def _reference_assets() -> list[ReferenceAsset]:
    return [
        ReferenceAsset(entity_id="group_001", uri="reference_assets/group_001.png"),
        ReferenceAsset(entity_id="location_001", uri="reference_assets/location_001.png"),
    ]


def _write_reference_files(reference_root: Any) -> None:
    asset_dir = reference_root / "reference_assets"
    asset_dir.mkdir(parents=True)
    (asset_dir / "group_001.png").write_bytes(b"group-reference")
    (asset_dir / "location_001.png").write_bytes(b"location-reference")


def test_storyboard_keyframes_run_in_parallel_and_use_only_shot_references(tmp_path: Any) -> None:
    reference_root = tmp_path / "phase4"
    _write_reference_files(reference_root)
    output_dir = tmp_path / "phase6" / "storyboard_keyframes"
    provider = ParallelReferenceProvider(expected_calls=2)

    keyframes = asyncio.run(
        generate_storyboard_keyframes(
            _frames(),
            _shots(),
            _reference_assets(),
            image_provider=provider,  # type: ignore[arg-type]
            reference_root=reference_root,
            output_dir=output_dir,
            model="gpt-image-2",
            size="1024x1536",
            quality="medium",
            input_fidelity="high",
        )
    )

    assert provider.started == 2
    assert provider.generate_calls == []
    assert [keyframe.model_dump() for keyframe in keyframes] == [
        {"shot_id": 1, "uri": "storyboard_keyframes/shot_001.png"},
        {"shot_id": 2, "uri": "storyboard_keyframes/shot_002.png"},
    ]

    first_call = next(call for call in provider.reference_calls if "Frame one" in call["prompt"])
    second_call = next(call for call in provider.reference_calls if "Frame two" in call["prompt"])
    assert [reference.content for reference in first_call["references"]] == [b"group-reference"]
    assert [reference.content for reference in second_call["references"]] == [
        b"group-reference",
        b"location-reference",
    ]
    assert first_call["input_fidelity"] == "high"
    assert "identity and appearance references" in first_call["prompt"]
    assert (output_dir / "shot_001.png").is_file()
    assert (output_dir / "shot_002.png").is_file()


def test_storyboard_keyframe_without_entities_uses_text_generation(tmp_path: Any) -> None:
    provider = ParallelReferenceProvider(expected_calls=1)
    frame = StoryboardFrame(shot_id=1, prompt="Empty landscape")
    shot = Shot(
        id=1,
        scene_id=1,
        beat_ids=[1],
        entity_ids=[],
        action="Show the landscape.",
    )

    asyncio.run(
        generate_storyboard_keyframes(
            [frame],
            [shot],
            [],
            image_provider=provider,  # type: ignore[arg-type]
            reference_root=tmp_path / "phase4",
            output_dir=tmp_path / "phase6" / "storyboard_keyframes",
            model="gpt-image-2",
            size="1024x1536",
            quality="low",
        )
    )

    assert len(provider.generate_calls) == 1
    assert provider.generate_calls[0]["prompt"] == "Empty landscape"
    assert provider.reference_calls == []


class FailingStoryboardProvider:
    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        if kwargs["prompt"] == "fail":
            raise RuntimeError("keyframe generation failed")
        return GeneratedImage(content=b"unused", media_type="image/png", extension="png")

    async def generate_image_with_references(self, **kwargs: Any) -> GeneratedImage:
        raise AssertionError("reference generation should not be called")


def test_storyboard_keyframe_failure_writes_no_partial_batch(tmp_path: Any) -> None:
    output_dir = tmp_path / "phase6" / "storyboard_keyframes"
    frames = [
        StoryboardFrame(shot_id=1, prompt="ok"),
        StoryboardFrame(shot_id=2, prompt="fail"),
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
                [],
                image_provider=FailingStoryboardProvider(),  # type: ignore[arg-type]
                reference_root=tmp_path / "phase4",
                output_dir=output_dir,
                model="gpt-image-2",
                size="1024x1536",
                quality="low",
            )
        )

    assert not output_dir.exists()


def test_storyboard_keyframe_missing_reference_fails_before_provider_call(tmp_path: Any) -> None:
    provider = ParallelReferenceProvider(expected_calls=1)

    with pytest.raises(ValueError, match="reference asset file not found"):
        asyncio.run(
            generate_storyboard_keyframes(
                [StoryboardFrame(shot_id=1, prompt="Frame")],
                [_shots()[0]],
                [ReferenceAsset(entity_id="group_001", uri="reference_assets/missing.png")],
                image_provider=provider,  # type: ignore[arg-type]
                reference_root=tmp_path / "phase4",
                output_dir=tmp_path / "phase6" / "storyboard_keyframes",
                model="gpt-image-2",
                size="1024x1536",
                quality="low",
            )
        )

    assert provider.started == 0
    assert provider.reference_calls == []
    assert provider.generate_calls == []
