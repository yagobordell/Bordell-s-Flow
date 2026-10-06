"""Offline tests for chained B3 narrator start-image generation."""

import base64
import hashlib
import io

from PIL import Image

from ai_video_factory.providers.b3_shots import generate_b3_shot_images
from ai_video_factory.providers.openai_batch_images import ImageOptions


def _png(size: tuple[int, int], value: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (value, value, value)).save(buffer, "PNG")
    return buffer.getvalue()


def test_b3_new_shots_reference_the_previous_effective_take(tmp_path) -> None:
    avatar = _png((24, 16), 10)
    (tmp_path / "avatar.png").write_bytes(avatar)
    calls: list[dict] = []

    class Direct:
        def generate_png(self, **kwargs):
            calls.append(kwargs)
            value = 40 + len(calls)
            raw = _png((1920, 1088), value)
            kwargs["destination"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["destination"].write_bytes(raw)
            return {
                "size": "1920x1088",
                "width": 1920,
                "height": 1088,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "usage": None,
                "created": 0,
            }

    plan = {
        "visual_style": "profesional",
        "avatar": {
            "file": "avatar.png",
            "sha256": hashlib.sha256(avatar).hexdigest(),
        },
        "image_assets": [],
        "blocks": [{
            "block_id": 1,
            "beats": [
                {
                    "beat_id": "1A", "visual_type": "avatar", "prompt": None,
                    "audio": True, "continuous": False,
                    "b3_shot_prompt": None,
                },
                {
                    "beat_id": "1B", "visual_type": "avatar", "prompt": None,
                    "audio": True, "continuous": False,
                    "b3_shot_prompt": "Same seated narrator, tighter three-quarter angle.",
                },
                {
                    "beat_id": "1C", "visual_type": "avatar", "prompt": None,
                    "audio": True, "continuous": True,
                    "b3_shot_prompt": None,
                },
                {
                    "beat_id": "1D", "visual_type": "avatar", "prompt": None,
                    "audio": True, "continuous": False,
                    "b3_shot_prompt": "Same seated narrator, closer frontal framing.",
                },
            ],
        }],
    }

    artifacts = generate_b3_shot_images(
        tmp_path,
        plan,
        api_key=None,
        options=ImageOptions(),
        direct_client=Direct(),
    )
    assert [item["beat_id"] for item in artifacts] == ["1B", "1D"]
    assert len(calls) == 2

    first_reference = base64.b64decode(
        calls[0]["reference_image_url"].split(",", 1)[1]
    )
    assert first_reference == avatar

    second_reference = base64.b64decode(
        calls[1]["reference_image_url"].split(",", 1)[1]
    )
    with Image.open(io.BytesIO(second_reference)) as image:
        assert image.size == (1920, 1080)
        assert image.getpixel((0, 0)) == (41, 41, 41)
