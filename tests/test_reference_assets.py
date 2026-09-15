import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.domain import VisualReference
from ai_video_factory.providers.images import GeneratedImage
from ai_video_factory.providers.openai_images import OpenAIImageProvider
from ai_video_factory.workflows.reference_assets import generate_reference_assets


class FakeImagesResource:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.last_call: dict[str, Any] | None = None

    async def generate(self, **kwargs: Any) -> Any:
        self.last_call = kwargs
        encoded = base64.b64encode(self.payload).decode("ascii")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])


class FakeOpenAIClient:
    def __init__(self, payload: bytes) -> None:
        self.images = FakeImagesResource(payload)


def test_openai_image_provider_decodes_png_and_forwards_generation_settings() -> None:
    client = FakeOpenAIClient(b"fake-png-bytes")
    provider = OpenAIImageProvider(client=client)  # type: ignore[arg-type]

    image = asyncio.run(
        provider.generate_image(
            prompt="Canonical samurai reference",
            model="gpt-image-2",
            size="1024x1024",
            quality="medium",
            output_format="png",
        )
    )

    assert image.content == b"fake-png-bytes"
    assert image.media_type == "image/png"
    assert image.extension == "png"
    assert client.images.last_call == {
        "model": "gpt-image-2",
        "prompt": "Canonical samurai reference",
        "n": 1,
        "size": "1024x1024",
        "quality": "medium",
        "output_format": "png",
    }


class ParallelImageProvider:
    def __init__(self, expected_calls: int) -> None:
        self.expected_calls = expected_calls
        self.started = 0
        self.all_started = asyncio.Event()

    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        self.started += 1
        if self.started == self.expected_calls:
            self.all_started.set()

        await asyncio.wait_for(self.all_started.wait(), timeout=0.5)
        prompt = str(kwargs["prompt"])
        return GeneratedImage(
            content=prompt.encode("utf-8"),
            media_type="image/png",
            extension="png",
        )


def test_reference_asset_workflow_runs_in_parallel_and_writes_deterministic_files(
    tmp_path: Any,
) -> None:
    references = [
        VisualReference(entity_id="group_001", prompt="Samurai group"),
        VisualReference(entity_id="location_001", prompt="Feudal Japan"),
    ]
    provider = ParallelImageProvider(expected_calls=2)
    output_dir = tmp_path / "phase4" / "reference_assets"

    assets = asyncio.run(
        generate_reference_assets(
            references,
            image_provider=provider,  # type: ignore[arg-type]
            output_dir=output_dir,
            model="gpt-image-2",
            size="1024x1024",
            quality="medium",
        )
    )

    assert provider.started == 2
    assert [asset.model_dump() for asset in assets] == [
        {
            "entity_id": "group_001",
            "uri": "reference_assets/group_001.png",
        },
        {
            "entity_id": "location_001",
            "uri": "reference_assets/location_001.png",
        },
    ]
    assert (output_dir / "group_001.png").read_bytes() == b"Samurai group"
    assert (output_dir / "location_001.png").read_bytes() == b"Feudal Japan"


class FailingImageProvider:
    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        if kwargs["prompt"] == "fail":
            raise RuntimeError("generation failed")
        return GeneratedImage(
            content=b"unused",
            media_type="image/png",
            extension="png",
        )


def test_reference_asset_workflow_writes_nothing_when_generation_fails(tmp_path: Any) -> None:
    output_dir = tmp_path / "phase4" / "reference_assets"

    with pytest.raises(RuntimeError, match="generation failed"):
        asyncio.run(
            generate_reference_assets(
                [
                    VisualReference(entity_id="group_001", prompt="ok"),
                    VisualReference(entity_id="group_002", prompt="fail"),
                ],
                image_provider=FailingImageProvider(),  # type: ignore[arg-type]
                output_dir=output_dir,
                model="gpt-image-2",
                size="1024x1024",
                quality="low",
            )
        )

    assert not output_dir.exists()


class SettlingFailingProvider:
    def __init__(self) -> None:
        self.finished: list[str] = []

    async def generate_image(self, **kwargs: Any) -> GeneratedImage:
        prompt = str(kwargs["prompt"])
        if prompt == "fail-fast":
            await asyncio.sleep(0.01)
            self.finished.append(prompt)
            raise RuntimeError("terminal generation failure")
        await asyncio.sleep(0.05)
        self.finished.append(prompt)
        return GeneratedImage(
            content=b"unused",
            media_type="image/png",
            extension="png",
        )


def test_reference_asset_workflow_settles_other_generations_before_raising(
    tmp_path: Any,
) -> None:
    provider = SettlingFailingProvider()
    output_dir = tmp_path / "phase4" / "reference_assets"

    with pytest.raises(RuntimeError, match="terminal generation failure"):
        asyncio.run(
            generate_reference_assets(
                [
                    VisualReference(entity_id="location_001", prompt="fail-fast"),
                    VisualReference(entity_id="location_002", prompt="finish-later"),
                ],
                image_provider=provider,  # type: ignore[arg-type]
                output_dir=output_dir,
                model="gpt-image-2",
                size="1024x1024",
                quality="low",
            )
        )

    assert provider.finished == ["fail-fast", "finish-later"]
    assert not output_dir.exists()
