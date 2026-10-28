"""Offline Title resume regression: image annotations must survive LTX retries."""
from __future__ import annotations

import copy

import pytest

from ai_video_factory.bots import title_flow
from scripts.pipeline.run_title_pipeline import _reconcile_visual_plan


def _base() -> dict:
    return {
        "schema_version": "b-pipeline-v1",
        "flow": "titulo",
        "visual_style": "profesional",
        "narrative_core": {
            "central_question": "Why the wrong number?",
            "final_answer": "A scripted lure",
        },
        "blocks": [{
            "block_id": 1, "beats": [{
                "beat_id": "1A", "text": "You see the message. ",
                "visual_type": "media_image", "prompt": "Phone on a table",
            }],
        }],
        "ebook_cta_groups": [["1C"]],
        "ebook_reference": {"file": "ebook/cover_1.png", "sha256": "cover-sha"},
        "avatar": {
            "filename": "Uncle_Enzo.png", "sha256": "avatar-sha",
            "voice": {"provider": "gemini", "voice_id": "Algenib"},
            "qr": {"sha256": "qr-sha", "url": "https://example.org"},
        },
    }


def _manifest() -> dict:
    return {
        "status": "completed", "finishing_status": "completed",
        "final_assets": [{
            "block_id": 1, "beat_id": "1A", "asset_id": "1A",
            "file": "images/final-1A.png", "sha256": "asset-sha",
            "provider": "openai",
        }],
        "items": [{
            "file": "images/grid-1.png", "sha256": "grid-sha",
            "provider": "openai", "size": "1536x1024",
            "grid": {"rows": 2, "cols": 2},
            "panels": {"1A": [0, 0]},
        }],
    }


def _enriched(base: dict) -> dict:
    return {
        **copy.deepcopy(base),
        "image_assets": [{
            "block_id": 1, "beat_id": "1A", "asset_id": "1A",
            "file": "images/final-1A.png", "sha256": "asset-sha",
            "provider": "openai",
        }],
        "image_grids": [{
            "file": "images/grid-1.png", "sha256": "grid-sha",
            "provider": "openai", "size": "1536x1024",
            "grid": {"rows": 2, "cols": 2},
            "panels": {"1A": [0, 0]},
        }],
    }


def _saved_enriched(tmp_path) -> tuple[dict, dict]:
    base = _base()
    manifest = _manifest()
    title_flow.write(tmp_path / "images" / "manifest.json", manifest)
    saved = _enriched(base)
    title_flow.write(tmp_path / "visual_plan.json", saved)
    return base, saved


def test_t2_only_plan_gains_avatar_on_first_full_run(tmp_path):
    base = _base()
    title_flow.write(
        tmp_path / "visual_plan.json",
        {k: v for k, v in base.items() if k != "avatar"},
    )
    assert _reconcile_visual_plan(tmp_path, base) == base
    assert title_flow.read(tmp_path / "visual_plan.json") == base


def test_existing_base_plan_is_not_rewritten(tmp_path):
    base = _base()
    path = tmp_path / "visual_plan.json"
    title_flow.write(path, base)
    previous = path.read_bytes()
    assert _reconcile_visual_plan(tmp_path, base) == base
    assert path.read_bytes() == previous


def test_restart_after_completed_images_preserves_visual_plan_byte_for_byte(tmp_path):
    base, saved = _saved_enriched(tmp_path)
    path = tmp_path / "visual_plan.json"
    before = path.read_bytes()
    assert _reconcile_visual_plan(tmp_path, base) == saved
    assert path.read_bytes() == before
    assert _reconcile_visual_plan(tmp_path, base) == saved
    assert path.read_bytes() == before


@pytest.mark.parametrize("edit", [
    "narration", "avatar", "voice", "qr", "cta", "unknown", "asset",
])
def test_resume_rejects_actual_mutation_and_forged_annotations(tmp_path, edit):
    base, saved = _saved_enriched(tmp_path)
    if edit == "narration":
        saved["blocks"][0]["beats"][0]["text"] = "Mutated beat. "
    elif edit == "avatar":
        saved["avatar"]["sha256"] = "another-avatar"
    elif edit == "voice":
        saved["avatar"]["voice"]["voice_id"] = "DifferentVoice"
    elif edit == "qr":
        saved["avatar"]["qr"]["sha256"] = "different-qr"
    elif edit == "cta":
        saved["ebook_cta_groups"] = [["other-beat"]]
    elif edit == "unknown":
        saved["new-untrusted-key"] = "data"
    elif edit == "asset":
        saved["image_assets"][0]["sha256"] = "untrusted-image"
    title_flow.write(tmp_path / "visual_plan.json", saved)
    with pytest.raises(title_flow.TitleContractError, match="immutable|annotations"):
        _reconcile_visual_plan(tmp_path, base)


@pytest.mark.parametrize("condition", ["missing", "incomplete", "different"])
def test_image_enriched_resume_rejects_invalid_manifest(tmp_path, condition):
    base, _ = _saved_enriched(tmp_path)
    path = tmp_path / "images" / "manifest.json"
    if condition == "missing":
        path.unlink()
    elif condition == "incomplete":
        manifest = title_flow.read(path)
        manifest["finishing_status"] = "running"
        title_flow.write(path, manifest)
    else:
        manifest = title_flow.read(path)
        manifest["final_assets"][0]["sha256"] = "other"
        title_flow.write(path, manifest)
    with pytest.raises(title_flow.TitleContractError, match="manifest|annotations"):
        _reconcile_visual_plan(tmp_path, base)
