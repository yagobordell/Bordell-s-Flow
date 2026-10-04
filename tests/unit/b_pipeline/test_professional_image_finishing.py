"""Offline contracts for professional image-only x2 and native video inputs."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image, ImageDraw

from ai_video_factory.compositor.static_preview import _verified_asset
from ai_video_factory.providers import openai_batch_images as gpt
from ai_video_factory.providers.grid_crops import crop_grid_assets
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.professional_image_finishing import (
    ProfessionalImageFinishingError,
    finish_professional_images,
)


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
    assert all(asset["file"] == f"images/block_1/{asset['beat_id']}.png" for asset in final)
    assert not (tmp_path / "images" / "professional_finished").exists()
    for asset in final:
        with Image.open(tmp_path / asset["file"]) as image:
            assert image.size == (asset["width"], asset["height"])
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
    # The paid grid stays intact; panel paths now hold only finished images.
    assert original["items"][0]["grid"]["width"] == 2560
    assert all(p["file"] == f"images/block_1/{p['beat_id']}.png" for p in finished)
    assert not (tmp_path / "images" / "professional_finished").exists()
    crop_grid_assets(tmp_path, original)  # Resume must accept verified replaced panels.
    gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )  # Reuse the original paid grid without any additional GPT request.
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


def test_completed_professional_finishing_is_archived_on_b2_rerun(tmp_path):
    from ai_video_factory.bots.billing import ApiCostLedger

    plan = professional(
        beat("1B", "media_image", "1. Forest"),
        beat("1C", "media_video", "2. Forest video"),
    )
    direct = RecordingGPT()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    crop_grid_assets(tmp_path, original)
    finish_professional_images(
        tmp_path, plan, original, upscaler=RecordingUpscaler(),
        api_key=None, direct_client=direct,
    )
    assert not gpt.has_pending_image_tasks(tmp_path)
    removed = gpt.clear_b2_images_for_rerun(tmp_path)
    assert removed >= 3
    archived = list((tmp_path / "images_replaced_professional_finishing").glob(
        "run-*/block_1/1C-native_grid_video.json"
    ))
    assert len(archived) == 1
    assert not (tmp_path / "images" / "finishing_state").exists()
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 0, "B1.2": 0, "B2": 0, "Fish director": 0},
    )
    assert report["stages"]["B2 images"]["priced_calls"] == 1
    assert "images_replaced_professional_finishing" in report["requests"][0][
        "image_state_file"
    ]


def test_b2_rerun_refuses_unresolved_native_video_edit_without_deletion(tmp_path):
    plan = professional(
        beat("1B", "media_image", "1. Forest"),
        beat("1C", "media_video", "2. Forest video"),
    )
    direct = RecordingGPT(fail_native=True)
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    crop_grid_assets(tmp_path, original)
    with pytest.raises(ConnectionError, match="lost native"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=RecordingUpscaler(),
            api_key=None, direct_client=direct,
        )
    paid_grid = tmp_path / "images" / "grids" / "block_1_1B.png"
    paid_bytes = paid_grid.read_bytes()
    assert gpt.has_pending_image_tasks(tmp_path)
    with pytest.raises(gpt.ImageGenerationError, match="finishing is unresolved"):
        gpt.clear_b2_images_for_rerun(tmp_path)
    assert paid_grid.read_bytes() == paid_bytes
    assert (tmp_path / "images" / "finishing_state" / "block_1"
            / "1C-native_grid_video.json").is_file()


def test_managed_partial_publication_contains_verified_professional_finished_images(tmp_path):
    from ai_video_factory.managed_media import _image_sources

    plan = professional(
        beat("1B", "media_image", "A still"),
        beat("1C", "media_video", "A moving scene"),
    )
    avatar = png((64, 64))
    (tmp_path / "avatar.png").write_bytes(avatar)
    plan["avatar"] = {
        "file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest(),
    }
    (tmp_path / "visual_plan.json").write_text(
        __import__("json").dumps(plan), encoding="utf-8",
    )
    direct = RecordingGPT()
    generated = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    finished = finish_professional_images(
        tmp_path, plan, generated, upscaler=RecordingUpscaler(),
        api_key=None, direct_client=direct,
    )
    run = {"image_generation": {"status": "completed", "count": len(generated["items"])}}
    status, files = _image_sources(tmp_path, run)
    assert status == "completed"
    for asset in finished:
        rel = asset["file"].removeprefix("images/")
        assert rel in files
        assert files[rel][1] == asset["sha256"]
    assert "avatar.png" in files
    # Publishing refuses a fake completed image report for a partially
    # finished professional run, rather than quietly exporting 720p originals.
    generated["finishing_status"] = "incomplete"
    (tmp_path / "images" / "manifest.json").write_text(
        __import__("json").dumps(generated), encoding="utf-8",
    )
    from ai_video_factory.publishing import PublicationError

    with pytest.raises(PublicationError, match="not finished"):
        _image_sources(tmp_path, run)



def test_ltx_selects_finished_professional_png_not_original_grid_or_crop(tmp_path):
    from ai_video_factory.workflows.b_video import _source_assets

    avatar = png((64, 64))
    original = png((1280, 720))
    finished = png((2560, 1440))
    (tmp_path / "avatar.png").write_bytes(avatar)
    old = tmp_path / "images" / "block_1" / "1B.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(finished)  # In-place replaces the original 720p image.
    plan = {
        "visual_style": "profesional",
        "avatar": {"file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest()},
    }
    images = {
        "status": "completed", "finishing_status": "completed",
        "items": [{
            "block_id": 1, "beat_id": "1B", "file": "images/block_1/1B.png",
            "sha256": hashlib.sha256(original).hexdigest(),
        }],
        "final_assets": [{
            "block_id": 1, "beat_id": "1B",
            "file": old.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(finished).hexdigest(),
        }],
    }
    _, assets = _source_assets(tmp_path, plan, images)
    assert assets[(1, "1B")] == old
    images["finishing_status"] = "incomplete"
    with pytest.raises(Exception, match="requires finished"):
        _source_assets(tmp_path, plan, images)


def test_professional_generates_paid_originals_before_salad_then_recovers(
    monkeypatch, tmp_path,
):
    import asyncio
    import json

    from scripts.pipeline import run_b_pipeline as runner

    direct = RecordingGPT()
    upscaler = RecordingUpscaler()
    plan = professional(beat("1B", "media_image", "Forest"))
    monkeypatch.setattr(
        runner, "generate_b2_images",
        lambda output, visual_plan, **kwargs: gpt.generate_b2_images(
            output, visual_plan, options=kwargs["options"], api_key=None,
            direct_client=direct,
        ),
    )
    monkeypatch.setattr(runner.settings, "image_upscaler_url", None)
    with pytest.raises(gpt.ImageGenerationError, match="GPT originals are saved"):
        asyncio.run(runner._generate_images(tmp_path, plan))

    original = tmp_path / "images" / "block_1" / "1B.png"
    paid_data = original.read_bytes()
    saved = json.loads((tmp_path / "images" / "manifest.json").read_text())
    assert saved["status"] == "completed"
    assert saved["finishing_status"] == "pending_upscaler"
    assert len(direct.calls) == 1
    assert runner._resume_unfinished_images(tmp_path, plan)

    # Subsequent --images-only must skip clearing saved originals. Once Salad
    # is available, the same plan safely reuses GPT state and finishes x2.
    calls = []
    monkeypatch.setattr(runner.settings, "image_upscaler_url", "https://gateway.salad.cloud")
    for setting in (
        "salad_api_key", "r2_endpoint_url", "r2_bucket",
        "r2_access_key_id", "r2_secret_access_key",
    ):
        monkeypatch.setattr(runner.settings, setting, "test-value")
    monkeypatch.setattr(
        runner, "ensure_image_upscaler_ready",
        lambda **kwargs: calls.append("salad_ready"),
    )
    monkeypatch.setattr(
        runner, "create_optium_image_upscaler",
        lambda **kwargs: upscaler,
    )
    result = asyncio.run(runner._generate_images(tmp_path, plan))
    assert calls == ["salad_ready"]
    assert original.read_bytes() != paid_data
    with Image.open(original) as image:
        assert image.size == (2560, 1440)
    assert len(direct.calls) == 1
    assert upscaler.calls == [((1280, 720), (2560, 1440))]
    assert result["finishing_status"] == "completed"
    assert not runner._resume_unfinished_images(tmp_path, plan)



def test_interrupted_atomic_replacement_recovers_without_second_paid_call(
    tmp_path, monkeypatch,
):
    from ai_video_factory.providers import professional_image_finishing as finishing

    plan = professional(beat("1B", "media_image", "A recovered photo"))
    direct = RecordingGPT()
    upscaler = RecordingUpscaler()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    paid_sha = original["items"][0]["sha256"]
    saved_json = finishing._save_json
    failed = False

    def fail_completed_state(path, data):
        nonlocal failed
        if (not failed and path.name == "1B-image_x2.json"
            and data.get("status") == "completed"):
            failed = True
            raise OSError("crash after replacing the original PNG")
        return saved_json(path, data)

    monkeypatch.setattr(finishing, "_save_json", fail_completed_state)
    with pytest.raises(OSError, match="crash after"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=upscaler, api_key=None,
        )
    image_path = tmp_path / "images" / "block_1" / "1B.png"
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() != paid_sha
    assert len(upscaler.calls) == 1

    # The GPT resume must accept the SHA-approved ready-to-replace state.
    regenerated = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    assert len(direct.calls) == 1
    monkeypatch.setattr(finishing, "_save_json", saved_json)
    finished = finish_professional_images(
        tmp_path, plan, regenerated, upscaler=upscaler, api_key=None,
    )
    assert finished[0]["file"] == "images/block_1/1B.png"
    assert len(upscaler.calls) == 1
    assert len(direct.calls) == 1
    assert not (tmp_path / "images" / "professional_finished").exists()


def test_professional_paid_originals_survive_transient_salad_start_error(
    monkeypatch, tmp_path,
):
    import asyncio
    import json

    from scripts.pipeline import run_b_pipeline as runner

    direct = RecordingGPT()
    plan = professional(beat("1B", "media_image", "Forest"))
    monkeypatch.setattr(
        runner, "generate_b2_images",
        lambda output, visual_plan, **kwargs: gpt.generate_b2_images(
            output, visual_plan, options=kwargs["options"], api_key=None,
            direct_client=direct,
        ),
    )
    for setting in (
        "salad_api_key", "r2_endpoint_url", "r2_bucket",
        "r2_access_key_id", "r2_secret_access_key",
    ):
        monkeypatch.setattr(runner.settings, setting, "test-value")
    monkeypatch.setattr(runner.settings, "image_upscaler_url", "https://gateway.salad.cloud")

    def reject_salad(**kwargs):
        assert (tmp_path / "images" / "block_1" / "1B.png").is_file()
        raise RuntimeError("Salad not ready")

    monkeypatch.setattr(runner, "ensure_image_upscaler_ready", reject_salad)
    with pytest.raises(gpt.ImageGenerationError, match="Salad not ready"):
        asyncio.run(runner._generate_images(tmp_path, plan))
    saved = json.loads((tmp_path / "images" / "manifest.json").read_text())
    assert saved["finishing_status"] == "pending_upscaler"
    assert runner._resume_unfinished_images(tmp_path, plan)
    assert len(direct.calls) == 1


def test_images_only_resume_keeps_incomplete_paid_states(tmp_path):
    import json

    from scripts.pipeline import run_b_pipeline as runner

    plan = professional(beat("1B", "media_image", "Forest"))
    manifest = tmp_path / "images" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "schema_version": "openai-b2-images-v1",
        "status": "incomplete",
    }), encoding="utf-8")
    assert runner._resume_unfinished_images(tmp_path, plan)
    manifest.write_text(json.dumps({
        "schema_version": "openai-b2-images-v1",
        "status": "completed",
        "finishing_status": "completed",
    }), encoding="utf-8")
    assert not runner._resume_unfinished_images(tmp_path, plan)
