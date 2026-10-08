"""Offline contracts for professional image-only x2 and native video inputs."""

from __future__ import annotations

import hashlib
import io
import json
import re

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
        grid = re.search(r"([23])x([23]) grid", kw["prompt"])
        if grid is not None:
            columns, rows = int(grid.group(1)), int(grid.group(2))
            image = Image.new("RGB", size, "white")
            drawing = ImageDraw.Draw(image)
            width, height = size[0] // columns, size[1] // rows
            for row in range(rows):
                for col in range(columns):
                    drawing.rectangle(
                        (col * width + 10, row * height + 10,
                         (col + 1) * width - 10, (row + 1) * height - 10),
                        fill=(50 + col * 65, 50 + row * 65, 90),
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
    assert (by_stem["1C"]["size"], by_stem["1C"]["quality"]) == ("1920x1088", "low")
    assert (by_stem["1D"]["size"], by_stem["1D"]["quality"]) == ("1920x1088", "low")
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



def test_professional_mixed_grid_upscales_only_stills_and_uses_video_crop(tmp_path):
    plan = professional(
        beat("1B", "media_image", "1. Red doorway"),
        beat("1C", "media_video", "2. Same doorway as moving shot"),
    )
    direct = RecordingGPT(fail_native=True)
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    assert original["grid_count"] == 1
    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == "3840x2160"
    assert direct.calls[0]["quality"] == "high"
    crop_grid_assets(tmp_path, original)
    crop_hashes = {p["beat_id"]: p["sha256"] for p in original["panel_assets"]}
    upscaler = RecordingUpscaler()
    finished = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler,
        api_key=None, direct_client=direct,
    )
    assert len(direct.calls) == 1  # Never create a second GPT image per panel.
    assert {
        p["beat_id"]: (p["width"], p["height"]) for p in finished
    } == {"1B": (3840, 2160), "1C": (1920, 1080)}
    assert finished[1]["sha256"] == crop_hashes["1C"]
    assert finished[1]["provider"] == "grid_video_crop"
    assert upscaler.calls == [((1920, 1080), (3840, 2160))]
    assert original["native_grid_video_calls"] == 0
    assert all(
        _verified_asset(tmp_path, p["file"], p["sha256"]).is_file() for p in finished
    )
    crop_grid_assets(tmp_path, original)
    gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    again = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler,
        api_key=None, direct_client=direct,
    )
    assert again == finished
    assert len(direct.calls) == 1
    assert len(upscaler.calls) == 1


def test_grid_video_finishing_requires_no_second_gpt_call_or_api_key(tmp_path):
    plan = professional(beat("1B", "media_video", "1. Forest action"),
                        beat("1C", "media_video", "2. Forest continuation"))
    direct = RecordingGPT(fail_native=True)
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    panels = crop_grid_assets(tmp_path, original)
    finished = finish_professional_images(
        tmp_path, plan, original, upscaler=None, api_key=None,
    )
    assert len(direct.calls) == 1
    assert [p["sha256"] for p in panels] == [p["sha256"] for p in finished]
    assert all(p["provider"] == "grid_video_crop" for p in finished)
    assert original["native_grid_video_calls"] == 0
    assert finish_professional_images(
        tmp_path, plan, original, upscaler=None, api_key=None,
    ) == finished

@pytest.mark.parametrize(("count", "canvas"), [
    (5, "3840x1440"),
    (7, "3840x2160"),
])
def test_large_grid_video_panels_stay_at_native_720p_without_gpt_edits(
    tmp_path, count, canvas,
):
    plan = professional(*[
        beat(f"1{chr(ord('B') + i)}", "media_video", f"{i+1}. Scene {i+1}")
        for i in range(count)
    ])
    direct = RecordingGPT(fail_native=True)
    manifest = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    assert len(direct.calls) == 1
    assert direct.calls[0]["size"] == canvas
    assert direct.calls[0]["quality"] == "high"
    panels = crop_grid_assets(tmp_path, manifest)
    assert len(panels) == count
    assert all((p["width"], p["height"]) == (1280, 720) for p in panels)
    finished = finish_professional_images(
        tmp_path, plan, manifest, upscaler=None, api_key=None,
    )
    assert len(direct.calls) == 1
    assert [f["sha256"] for f in finished] == [p["sha256"] for p in panels]
    assert all((p["width"], p["height"]) == (1280, 720) for p in finished)
    assert manifest["native_grid_video_calls"] == 0


def test_professional_standalone_video_uses_context_dependent_quality(tmp_path):
    plan = professional(
        beat("1B", "media_video", "An ordinary street view"),
        beat("1C", "media_video",
             "A laptop on a table. ( For context : quarterly results.)"),
    )
    direct = RecordingGPT()
    manifest = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    by_beat = {call["destination"].stem: call for call in direct.calls}
    assert by_beat["1B"]["quality"] == "low"
    assert by_beat["1C"]["quality"] == "medium"
    assert all(call["size"] == "1920x1088" for call in direct.calls)
    assert {item["beat_id"]: item["quality"] for item in manifest["items"]} == {
        "1B": "low", "1C": "medium",
    }


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



def test_professional_grid_sizes_depend_on_video_and_canvas_limit():
    cases = [
        (["media_image", "media_image"], "2560x1440", (1280, 720)),
        (["media_image", "media_video"], "3840x2160", (1920, 1080)),
        (["media_image", "media_image", "media_video"], "3840x2160", (1920, 1080)),
        (["media_image"] * 4 + ["media_video"], "3840x1440", (1280, 720)),
        (["media_image"] * 6 + ["media_video"], "3840x2160", (1280, 720)),
    ]
    for kinds, expected_size, expected_panel in cases:
        plan = professional(*[
            beat(f"1{chr(ord('B') + i)}", kind, f"{i+1}. Step {i+1}")
            for i, kind in enumerate(kinds)
        ])
        jobs = plan_image_jobs(plan, gpt.described_beats(plan))
        assert len(jobs) == 1
        assert jobs[0]["size"] == expected_size
        assert (jobs[0]["grid"]["panel_width"],
                jobs[0]["grid"]["panel_height"]) == expected_panel


def test_video_selected_as_grid_filler_increases_2x2_canvas():
    plan = professional(
        beat("1B", "media_image", "1. First image"),
        beat("1C", "media_image", "2. Second image"),
        beat("1D", "media_video", "An unnumbered video filler"),
    )
    jobs = plan_image_jobs(plan, gpt.described_beats(plan))
    assert jobs[0]["size"] == "3840x2160"
    assert (jobs[0]["grid"]["panel_width"],
            jobs[0]["grid"]["panel_height"]) == (1920, 1080)


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
    assert not (tmp_path / "images" / "finishing_state").exists()
    assert list((tmp_path / "images_replaced_professional_finishing").glob(
        "run-*/block_1/1B-image_x2.json"
    ))
    assert list((tmp_path / "images_replaced_b2").glob(
        "run-*/grids/block_1_1B.json"
    ))
    ledger = ApiCostLedger()
    ledger.sync_image_usage(tmp_path)
    report = ledger.report(
        blocks=0, run_status="completed",
        expected_calls={"B1.1": 0, "B1.2": 0, "B2": 0, "Fish director": 0},
    )
    assert report["stages"]["B2 images"]["priced_calls"] == 1
    assert "images_replaced_b2" in report["requests"][0]["image_state_file"]


def test_historical_unknown_native_edit_is_not_discarded(tmp_path):
    plan = professional(
        beat("1B", "media_image", "1. Forest"),
        beat("1C", "media_video", "2. Forest video"),
    )
    direct = RecordingGPT()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    crop_grid_assets(tmp_path, original)
    state = tmp_path / "images" / "finishing_state" / "block_1" / "1C-native_grid_video.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "schema_version": "professional-image-finishing-v1",
        "status": "direct_submitting_unknown",
        "source": "images/block_1/1C.png",
    }))
    with pytest.raises(ProfessionalImageFinishingError, match="Historical"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=RecordingUpscaler(), api_key=None,
        )
    paid_grid = tmp_path / "images" / "grids" / "block_1_1B.png"
    paid_bytes = paid_grid.read_bytes()
    assert gpt.has_pending_image_tasks(tmp_path)
    with pytest.raises(gpt.ImageGenerationError, match="finishing is unresolved"):
        gpt.clear_b2_images_for_rerun(tmp_path)
    assert paid_grid.read_bytes() == paid_bytes
    assert state.is_file()

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
    from contextlib import contextmanager

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
        "r2_access_key_id", "r2_secret_access_key", "postgres_dsn",
    ):
        monkeypatch.setattr(runner.settings, setting, "test-value")

    @contextmanager
    def ready_upscaler_usage(**kwargs):
        del kwargs
        calls.append("salad_ready")
        yield

    monkeypatch.setattr(runner, "image_upscaler_usage", ready_upscaler_usage)
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


def test_legacy_finished_image_migrates_without_another_paid_call(tmp_path):
    import json

    from ai_video_factory.providers import professional_image_finishing as finishing

    plan = professional(beat("1B", "media_image", "Legacy photo"))
    direct = RecordingGPT()
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=direct,
    )
    source = original["items"][0]
    identity = {
        "profile": finishing._PROFILE, "recipe": "image_x2",
        "source_sha256": source["sha256"], "target": (2560, 1440),
        "model": None, "avatar_reference_sha256": None,
        "prompt_sha256": "exact-image-x2",
    }
    identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    legacy = tmp_path / "images" / "professional_finished" / "block_1"
    legacy.mkdir(parents=True)
    old_png = legacy / "1B-image_x2.png"
    old_png.write_bytes(png((2560, 1440)))
    (legacy / "1B-image_x2.json").write_text(json.dumps({
        "schema_version": "professional-image-finishing-v1",
        "identity": identity_hash, "status": "completed",
        "source": source["file"], "block_id": 1, "beat_id": "1B",
        "sha256": hashlib.sha256(old_png.read_bytes()).hexdigest(),
    }), encoding="utf-8")

    upscaler = RecordingUpscaler()
    finished = finish_professional_images(
        tmp_path, plan, original, upscaler=upscaler, api_key=None,
    )
    assert finished[0]["file"] == "images/block_1/1B.png"
    assert (tmp_path / finished[0]["file"]).read_bytes() == png((2560, 1440))
    assert not (tmp_path / "images" / "professional_finished").exists()
    assert upscaler.calls == []
    assert len(direct.calls) == 1


def test_managed_partial_image_checkpoint_uses_recovered_final_sha(tmp_path, monkeypatch):
    import json

    from ai_video_factory.managed_media import _image_sources
    from ai_video_factory.providers import professional_image_finishing as finishing

    plan = professional(beat("1B", "media_image", "A partial photo"))
    avatar = png((64, 64))
    (tmp_path / "avatar.png").write_bytes(avatar)
    plan["avatar"] = {
        "file": "avatar.png", "sha256": hashlib.sha256(avatar).hexdigest(),
    }
    (tmp_path / "visual_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    original = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(), direct_client=RecordingGPT(),
    )
    saved_json = finishing._save_json

    def interrupted(path, data):
        if path.name == "1B-image_x2.json" and data.get("status") == "completed":
            raise OSError("interrupted final-state write")
        return saved_json(path, data)

    monkeypatch.setattr(finishing, "_save_json", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        finish_professional_images(
            tmp_path, plan, original, upscaler=RecordingUpscaler(), api_key=None,
        )
    status, files = _image_sources(tmp_path, {})
    assert status == "incomplete"
    image = tmp_path / "images" / "block_1" / "1B.png"
    assert files["block_1/1B.png"][1] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert files["block_1/1B.png"][1] != original["items"][0]["sha256"]


def test_professional_paid_originals_survive_transient_salad_start_error(
    monkeypatch, tmp_path,
):
    import asyncio
    import json
    from contextlib import contextmanager

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
        "r2_access_key_id", "r2_secret_access_key", "postgres_dsn",
    ):
        monkeypatch.setattr(runner.settings, setting, "test-value")
    monkeypatch.setattr(runner.settings, "image_upscaler_url", "https://gateway.salad.cloud")
    monkeypatch.setattr(
        runner,
        "create_optium_image_upscaler",
        lambda **kwargs: RecordingUpscaler(),
    )

    @contextmanager
    def reject_salad(**kwargs):
        del kwargs
        assert (tmp_path / "images" / "block_1" / "1B.png").is_file()
        raise runner.ImageUpscalerStartupError("Salad not ready")
        yield  # pragma: no cover

    monkeypatch.setattr(runner, "image_upscaler_usage", reject_salad)
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



def test_images_only_replays_only_same_ambiguous_optium_x2_request(tmp_path):
    from ai_video_factory.providers.optium_image_upscaler import (
        ImageUpscalerError,
        OptiumLabsImageUpscaler,
    )

    class StubOptium(OptiumLabsImageUpscaler):
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.calls = 0

        def upscale(self, image_bytes, *, target_width, target_height):
            self.calls += 1
            if self.fail:
                raise ImageUpscalerError("HTTP 502", status_code=502)
            return RecordingUpscaler().upscale(
                image_bytes, target_width=target_width, target_height=target_height,
            )

    plan = professional(beat("1B", "media_image", "Landscape"))
    direct = RecordingGPT()
    manifest = gpt.generate_b2_images(
        tmp_path, plan, api_key=None, options=gpt.ImageOptions(),
        direct_client=direct,
    )
    original = tmp_path / "images" / "block_1" / "1B.png"
    source_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    failed = StubOptium(fail=True)
    with pytest.raises(ImageUpscalerError, match="502"):
        finish_professional_images(
            tmp_path, plan, manifest, upscaler=failed, api_key=None,
        )
    assert failed.calls == 1
    saved = list((tmp_path / "images" / "finishing_state").rglob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["status"] == "upscale_submitting_unknown"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == source_sha

    recovered = StubOptium(fail=False)
    with pytest.raises(ProfessionalImageFinishingError, match="Unknown outcome"):
        finish_professional_images(
            tmp_path, plan, manifest, upscaler=recovered, api_key=None,
        )
    assert recovered.calls == 0

    result = finish_professional_images(
        tmp_path, plan, manifest, upscaler=recovered, api_key=None,
        allow_ambiguous_upscale_resume=True,
    )
    assert result[0]["width"] == 2560
    assert recovered.calls == 1
    assert len(direct.calls) == 1
    assert json.loads(saved[0].read_text())["status"] == "completed"
