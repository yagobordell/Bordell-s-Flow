"""Offline contracts for professional image-only x2 and native video inputs."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image, ImageDraw

from ai_video_factory.providers import openai_batch_images as gpt
from ai_video_factory.providers.grid_crops import crop_grid_assets
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.professional_image_finishing import (
    ProfessionalImageFinishingError,
    finish_professional_images,
)
from ai_video_factory.compositor.static_preview import _verified_asset


def png(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (80, 110, 145)).save(out, "PNG")
    return out.getvalue()


def professional(*beats):
    return {
        "visual_style": "profesional",
        "blocks": [{"block_id": 1, "beats": [
            {"beat_id": "1A", "visual_type": "avatar", "prompt": None,
             "reference": False, "audio": True, "text": "Opening"},
            *beats,
        ]}],
    }


def beat(ident: str, kind: str, prompt: str, *, reference=False, audio=False):
    return {
        "beat_id": ident, "visual_type": kind, "prompt": prompt,
        "reference": reference, "audio": audio, "text": "Test narration.",
    }


class RecordingGPT:
    def __init__(self, *, fail_native=False):
        self.calls = []
        self.fail_native = fail_native

    def generate_png(self, **kw):
        self.calls.append(kw)
        if self.fail_native and kw.get("reference_image_urls"):
            raise ConnectionError("lost native grid edit outcome")
        size = tuple(map(int, kw["size"].split("x")))
        destination = kw["destination"]
        if size == (2560, 1440) and not kw.get("reference_image_urls"):
            # Non-uniform grid with real straight white separators.
            image = Image.new("RGB", size, "white")
            drawing = ImageDraw.Draw(image)
            for row in range(2):
                for col in range(2):
                    drawing.rectangle(
                        (col * 1280 + 10, row * 720 + 10,
                         (col + 1) * 1280 - 10, (row + 1) * 720 - 10),
                        fill=(50 + col * 80, 50 + row * 100, 90),
                    )
            out = io.BytesIO()
            image.save(out, "PNG")
            data = out.getvalue()
        else:
            data = png(size)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": kw["size"], "width": size[0], "height": size[1],
            "usage": {
                "input_tokens": 120,
                "input_tokens_details": {"text_tokens": 100, "image_tokens": 20},
                "output_tokens": 1000,
            },
            "created": 1,
        }


class RecordingUpscaler:
    def __init__(self):
        self.calls = []

    def upscale(self, image_bytes, *, target_width, target_height):
        with Image.open(io.BytesIO(image_bytes)) as image:
            origin = image.size
            resized = image.resize((target_width, target_height), Image.Resampling.NEAREST)
            out = io.BytesIO()
            resized.save(out, format="PNG")
        self.calls.append((origin, (target_width, target_height)))
        return out.getvalue()


def test_professional_standalone_native_video_still_image_original_quality_unchanged(
    tmp_path,
):
    plan = professional(
        beat("1B", "media_image", "Photo"),
        beat("1C", "media_video", "Video first frame"),
        beat("1D", "avatar", "Presenter speaking", reference=True, audio=True),
    )
    # No reference needed for first two, but the third must have a verified
    # saved avatar to exercise the existing edit endpoint contract.
    original_avatar = png((160, 96))
    (tmp_path / "avatar.png").write_bytes(original_avatar)
    plan["avatar"] = {
        "file": "avatar.png", "sha256": hashlib.sha256(original_avatar).hexdigest(),
    }
    direct = RecordingGPT()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    by_stem = {call["destination"].stem: call for call in direct.calls}
    assert (by_stem["1B"]["size"], by_stem["1B"]["quality"]) == ("1280x720", "low")
    assert (by_stem["1C"]["size"], by_stem["1C"]["quality"]) == ("1920x1088", "medium")
    assert (by_stem["1D"]["size"], by_stem["1D"]["quality"]) == ("1920x1088", "medium")
    assert by_stem["1D"]["reference_image_url"].startswith("data:image/png;base64,")
    original_hashes = [item["sha256"] for item in original["items"]]
    upscaler = RecordingUpscaler()
    final = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler, api_key=None,
        direct_client=direct,
    )
    assert upscaler.calls == [((1280, 720), (2560, 1440))]
    assert {
        item["beat_id"]: (item["width"], item["height"]) for item in final
    } == {"1B": (2560, 1440), "1C": (1920, 1080), "1D": (1920, 1080)}
    assert [item["sha256"] for item in original["items"]] == original_hashes
    assert len(direct.calls) == 3
    assert finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler,
        api_key=None, direct_client=direct,
    ) == final
    assert len(upscaler.calls) == 1 and len(direct.calls) == 3


def test_professional_mixed_grid_upscales_images_and_natively_regenerates_video(
    tmp_path,
):
    plan = professional(
        beat("1B", "media_image", "1. Red doorway"),
        beat("1C", "media_video", "2. Same doorway as moving shot"),
    )
    direct = RecordingGPT()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    assert original["grid_count"] == 1
    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == "2560x1440"
    assert direct.calls[0]["quality"] == "medium"
    crop_grid_assets(tmp_path, original)
    crop_hashes = [p["sha256"] for p in original["panel_assets"]]
    upscaler = RecordingUpscaler()
    finished = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler,
        api_key=None, direct_client=direct,
    )
    assert len(direct.calls) == 2
    video_edit = direct.calls[1]
    assert video_edit["size"] == "1920x1088"
    assert video_edit["quality"] == "medium"
    assert len(video_edit["reference_image_urls"]) == 1
    assert video_edit["destination"].suffix == ".png"
    assert {
        p["beat_id"]: (p["width"], p["height"]) for p in finished
    } == {"1B": (2560, 1440), "1C": (1920, 1080)}
    assert upscaler.calls == [((1280, 720), (2560, 1440))]
    assert original["native_grid_video_calls"] == 1
    assert [p["sha256"] for p in original["panel_assets"]] == crop_hashes
    assert all(
        _verified_asset(tmp_path, p["file"], p["sha256"]).is_file() for p in finished
    )
    # Crops and paid grids remain independently auditable.
    assert original["items"][0]["grid"]["width"] == 2560
    again = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler,
        api_key=None, direct_client=direct,
    )
    assert again == finished
    assert len(direct.calls) == 2
    assert len(upscaler.calls) == 1


def test_lost_paid_native_grid_edit_is_not_resubmitted_on_resume(tmp_path):
    plan = professional(
        beat("1B", "media_image", "1. Forest"),
        beat("1C", "media_video", "2. Forest action"),
    )
    direct = RecordingGPT(fail_native=True)
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    crop_grid_assets(tmp_path, original)
    upscaler = RecordingUpscaler()
    with pytest.raises(ConnectionError, match="lost native"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=upscaler,
            api_key=None, direct_client=direct,
        )
    assert original["finishing_status"] == "incomplete"
    assert len(direct.calls) == 2
    with pytest.raises(ProfessionalImageFinishingError, match="Unknown outcome"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=upscaler,
            api_key=None, direct_client=direct,
        )
    assert len(direct.calls) == 2
    assert len(upscaler.calls) == 1


def test_casero_image_jobs_do_not_acquire_new_resolution_or_finishing(tmp_path):
    plan = {"blocks": [{"block_id": 1, "beats": [
        {"beat_id": "1A", "visual_type": "avatar", "description": None},
        {"beat_id": "1B", "visual_type": "media_image", "description": "Photo"},
        {"beat_id": "1C", "visual_type": "media_video", "description": "Video"},
    ]}]}
    direct = RecordingGPT()
    saved = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    assert [call["size"] for call in direct.calls] == ["1280x720", "1280x720"]
    assert saved.get("finishing_status") is None
    assert saved.get("final_assets") is None
    with pytest.raises(ProfessionalImageFinishingError):
        finish_professional_images(
            tmp_path, plan, saved, upscaler=RecordingUpscaler(),
            api_key=None, direct_client=direct,
        )


def test_mixed_grid_video_resolution_comes_from_native_edit_not_resize():
    plan = professional(
        beat("1B", "media_image", "1. Photo"),
        beat("1C", "media_video", "2. Video"),
    )
    described = gpt.described_beats(plan)
    jobs = plan_image_jobs(plan, described)
    assert len(jobs) == 1
    assert jobs[0]["size"] == "2560x1440"
    assert jobs[0]["video_panel_ids"] == ["1C"]
    assert jobs[0]["grid"]["panel_width"] == 1280
    # Native per-video output is a separately paid edit after cropping.
