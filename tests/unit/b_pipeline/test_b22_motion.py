"""Image-grounded I2V-only B2.2 adaptation and resume behavior."""

import json
from types import SimpleNamespace

import pytest

from ai_video_factory.workflows.b22_motion import (
    B22Beat,
    B22MotionError,
    B22Result,
    adapt_b22_motion,
)


class FakeProvider:
    def __init__(self, *, wrong_ids=False):
        self.calls = []
        self.wrong_ids = wrong_ids

    async def generate_structured_with_response(self, **kwargs):
        self.calls.append(kwargs)
        manifest = json.loads(kwargs["input_text"].split("\n", 1)[1])
        ids = manifest["expected_output_beat_ids"]
        if self.wrong_ids:
            ids = list(reversed(ids))
        return B22Result(beats=[
            B22Beat(beat_id=beat_id, video_prompt=f"Adapted {beat_id} motion.")
            for beat_id in ids
        ]), SimpleNamespace(usage=None)


def _job(image, beat_ids, kind, prompt):
    return SimpleNamespace(
        beats=tuple(beat_ids),
        kind=kind,
        duration_seconds=3.0,
        image=image,
        request=SimpleNamespace(parameters={"prompt": prompt}),
    )


def _beat(beat_id, kind="media_video", *, audio=False, prompt="Move naturally."):
    return {
        "beat_id": beat_id, "block_id": int(beat_id[0]),
        "beat_type": "action", "text": f"Beat {beat_id} speech.",
        "visual_type": kind, "audio": audio, "video_prompt": prompt,
        "start_seconds": 0.0, "end_seconds": 3.0,
    }


def _image(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-pixel-data-" + name.encode())
    return path


def test_b22_adapts_i2v_and_non_avatar_a2v_with_whole_group_context(tmp_path):
    first = _image(tmp_path, "first.png")
    middle = _image(tmp_path, "middle.png")
    third = _image(tmp_path, "third.png")
    avatar = _image(tmp_path, "avatar.png")
    timeline = {"beats": [
        _beat("1A"),
        _beat("1B", "media_image", prompt=None),
        _beat("1C", audio=False),
        _beat("1D", "avatar", audio=True, prompt=None),
    ]}
    jobs = [
        _job(first, ("1A",), "i2v", "Move naturally."),
        _job(third, ("1C",), "i2v", "Gesture."),
        _job(avatar, ("1D",), "a2v", "A Person speaking to the camera."),
    ]
    fake = FakeProvider()
    prompts = adapt_b22_motion(
        tmp_path, timeline, jobs, {(1, "1B"): middle},
        provider=fake, model="unit-test-model",
    )
    assert prompts == {
        "1A": "Adapted 1A motion.",
        "1C": "Adapted 1C motion.",
    }
    assert len(fake.calls) == 1
    arguments = fake.calls[0]
    manifest = json.loads(arguments["input_text"].split("\n", 1)[1])
    assert [row["beat_id"] for row in manifest["timeline"]] == [
        "1A", "1B", "1C", "1D",
    ]
    assert [row["output_beat_id"] for row in manifest["shots"]] == [
        "1A", "1C", "1D",
    ]
    assert manifest["expected_output_beat_ids"] == ["1A", "1C"]
    assert manifest["context_images"][0]["beat_id"] == "1B"
    assert len(arguments["input_image_urls"]) == 4


def test_b22_a2v_uses_first_beat_of_take_and_post_b3_override(tmp_path):
    image = _image(tmp_path, "avatar.png")
    timeline = {"beats": [
        _beat("1A", "avatar_media", audio=True, prompt=None),
        _beat("1B", "avatar", audio=True, prompt=None),
    ]}
    job = _job(image, ("1A", "1B"), "a2v", "A Person speaking to the camera.")
    fake = FakeProvider()
    assert adapt_b22_motion(
        tmp_path, timeline, [job], {}, provider=fake, model="test",
    ) == {}
    assert not fake.calls

def test_b22_resume_cache_and_invalidation_by_prompt_and_image(tmp_path):
    image = _image(tmp_path, "frame.png")
    timeline = {"beats": [_beat("1A")]}
    fake = FakeProvider()
    job = _job(image, ("1A",), "i2v", "Look at the phone.")
    call = lambda x: adapt_b22_motion(  # noqa: E731
        tmp_path, timeline, [x], {}, provider=fake, model="test",
    )
    assert call(job) == {"1A": "Adapted 1A motion."}
    assert call(job) == {"1A": "Adapted 1A motion."}
    assert len(fake.calls) == 1

    changed = _job(image, ("1A",), "i2v", "Raise the phone.")
    call(changed)
    assert len(fake.calls) == 2

    image.write_bytes(image.read_bytes() + b"changed")
    call(changed)
    assert len(fake.calls) == 3


def test_b22_malformed_or_missing_ids_never_reach_ltx(tmp_path):
    image = _image(tmp_path, "frame.png")
    timeline = {"beats": [_beat("1A"), _beat("1B")]}
    jobs = [
        _job(image, ("1A",), "i2v", "One movement."),
        _job(image, ("1B",), "i2v", "Other movement."),
    ]
    fake = FakeProvider(wrong_ids=True)
    with pytest.raises(B22MotionError, match="beat IDs/order"):
        adapt_b22_motion(
            tmp_path, timeline, jobs, {}, provider=fake, model="test",
        )
    assert len(fake.calls) == 2


def test_b22_pure_avatar_has_no_model_cost(tmp_path):
    avatar = _image(tmp_path, "avatar.png")
    timeline = {"beats": [_beat("1A", "avatar", audio=True, prompt=None)]}
    fake = FakeProvider()
    assert adapt_b22_motion(
        tmp_path, timeline, [
            _job(avatar, ("1A",), "a2v", "A Person speaking to the camera."),
        ], {}, provider=fake, model="test",
    ) == {}
    assert fake.calls == []
