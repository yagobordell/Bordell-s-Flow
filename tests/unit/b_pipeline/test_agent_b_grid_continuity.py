"""Phase 3: cross-block logical A/B continuity and real PNG dependency fixtures."""

import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from ai_video_factory.providers.image_grids import GridPlanError, plan_image_jobs
from ai_video_factory.providers.openai_batch_images import (
    ImageOptions,
    described_beats,
    generate_b2_images,
)


def _beat(block_id, suffix, text, prompt, *, subshots=None):
    beat = {
        "beat_id": f"{block_id}{suffix}",
        "beat_type": "action",
        "text": text,
        "visual_type": "media_image",
        "prompt": prompt,
        "video_prompt": None,
        "audio": False,
        "reference": False,
    }
    if subshots is not None:
        beat["visual_subshots"] = subshots
    return beat


def _block(block_id, beats):
    return {"block_id": block_id, "type": "development", "beats": beats}


def _interleaved():
    return {
        "schema_version": "b-pipeline-v1", "visual_style": "profesional",
        "blocks": [
            _block(1, [_beat(1, "A", "Call.", "A1. A caller in a city apartment."),
                       _beat(1, "B", "Look.", "A2. The same caller checking the phone.")]),
            _block(2, [_beat(2, "A", "Turn.", "B1. Another person in a bank office."),
                       _beat(2, "B", "Wait.", "B2. The second person standing by a window.")]),
            _block(3, [_beat(3, "A", "Back.", "A3. Back with the first caller and phone.")]),
        ],
    }


def test_interleaved_logical_chains_make_chronological_jobs_and_one_png_dependency():
    plan = _interleaved()
    jobs = plan_image_jobs(plan, described_beats(plan))
    assert len(jobs) == 3
    assert [[panel["description"] for panel in job["panels"]
             if panel["source"] != "black_panel"] for job in jobs if job.get("grid")] == [
                 ["A1. A caller in a city apartment.",
                  "A2. The same caller checking the phone."],
                 ["B1. Another person in a bank office.",
                  "B2. The second person standing by a window."],
             ]
    assert [job["continuity_chain_id"] for job in jobs] == ["A", "B", "A"]
    assert jobs[2].get("grid") is None
    assert jobs[2]["continuity_previous_asset_id"] == "1A"
    assert jobs[2]["continuity_previous_block_id"] == 1
    assert jobs[2]["continuity_index"] == 1
    assert "Logical continuity identities" in jobs[0]["description"]
    assert "panel 1 = A1" in jobs[0]["description"]
    assert jobs[0]["grid"]["numbered_asset_ids"] == ["1A", "1B"]


def test_old_numeric_grids_keep_old_planner_and_rendering():
    old = {
        "visual_style": "profesional",
        "blocks": [_block(1, [_beat(1, "A", "First.", "1. A first shot."),
                              _beat(1, "B", "Second.", "2. A second shot.")])],
    }
    jobs = plan_image_jobs(old, described_beats(old))
    assert len(jobs) == 1
    assert jobs[0]["grid"]["numbered_asset_ids"] == ["1A", "1B"]


def test_new_agent_rejects_legacy_numeric_and_skipped_sequence_ordinal():
    plan = _interleaved()
    plan["blocks"][0]["beats"][0]["prompt"] = "1. Old panel numbering."
    with pytest.raises(GridPlanError, match="old numeric"):
        plan_image_jobs(plan, described_beats(plan))
    plan = _interleaved()
    plan["blocks"][2]["beats"][0]["prompt"] = "A5. Skipped A3."
    with pytest.raises(GridPlanError, match="expected A3"):
        plan_image_jobs(plan, described_beats(plan))


def test_paid_png_reference_crosses_block_without_recreating_previous_image(tmp_path):
    plan = _interleaved()
    requests = []

    class FakeImages:
        def generate_png(self, **kwargs):
            requests.append(kwargs)
            width, height = map(int, kwargs["size"].split("x"))
            output = BytesIO()
            Image.new("RGB", (width, height), (25 + len(requests), 35, 45)).save(
                output, format="PNG",
            )
            binary = output.getvalue()
            destination = kwargs["destination"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(binary)
            return {
                "sha256": hashlib.sha256(binary).hexdigest(),
                "width": width, "height": height, "size": kwargs["size"],
                "usage": None,
            }

    fake = FakeImages()
    args = {"api_key": None, "options": ImageOptions(max_parallel_images=1),
            "direct_client": fake}
    first = generate_b2_images(tmp_path, plan, **args)
    assert first["status"] == "completed"
    assert len(requests) == 3
    assert "reference_image_urls" in requests[2]
    final_reference = requests[2]["reference_image_urls"][-1]
    reference_png = base64.b64decode(final_reference.split(",", 1)[1])
    root_grid = next(item for item in first["items"]
                     if item.get("continuity_chain_id") == "A"
                     and item.get("continuity_index") == 0)
    assert reference_png == (tmp_path / root_grid["file"]).read_bytes()
    assert "CONTINUES" in requests[2]["prompt"]
    second = generate_b2_images(tmp_path, plan, **args)
    assert second["status"] == "completed"
    assert len(requests) == 3


def test_two_stills_one_beat_share_one_physical_grid():
    a = _beat(1, "A", "A" * 70, "A1. First physical subshot.")
    a["visual_subshots"] = [
        {"asset_id": "1A__1", "subshot_index": 1, "subshot_count": 2,
         "prompt": "A1. First physical subshot.", "generation": "grid"},
        {"asset_id": "1A__2", "subshot_index": 2, "subshot_count": 2,
         "prompt": "A2. Second physical subshot.", "generation": "grid"},
    ]
    plan = {"schema_version": "b-pipeline-v1", "visual_style": "profesional",
            "blocks": [_block(1, [a])]}
    jobs = plan_image_jobs(plan, described_beats(plan))
    assert len(jobs) == 1
    assert jobs[0]["grid"]["numbered_asset_ids"] == ["1A__1", "1A__2"]
