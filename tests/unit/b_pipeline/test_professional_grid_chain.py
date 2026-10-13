"""Professional logical sequences support 20 panels across grids of up to 9."""

import hashlib
from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from ai_video_factory.bots.professional_cta import ProfessionalCTAVisualBeat
from ai_video_factory.providers.grid_numbering import parse_grid_marker
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import (
    ImageOptions,
    described_beats,
    generate_b2_images,
)


def _plan(count):
    assert 2 <= count <= 20
    beats = [{
        "block_id": 1,
        "beat_id": f"1{chr(ord('A') + i)}",
        "visual_type": "media_image",
        "text": f"Step {i + 1}",
        "prompt": f"A{i + 1}. A person continues the same event, step {i + 1}.",
        "video_prompt": None,
        "audio": False,
        "reference": False,
    } for i in range(count)]
    return {
        "visual_style": "profesional",
        "blocks": [{"block_id": 1, "type": "development", "beats": beats}],
    }


@pytest.mark.parametrize("count", [2, 3, 4, 5, 9, 10, 17, 18, 19, 20])
def test_lettered_logical_run_of_up_to_twenty_panels_packs_into_grids_of_nine(count):
    plan = _plan(count)
    jobs = plan_image_jobs(plan, described_beats(plan))
    grids = [job for job in jobs if job.get("grid")]
    assert all(2 <= len(job["grid"]["numbered_asset_ids"]) <= 9 for job in grids)
    assert sum(len(job["grid"]["numbered_asset_ids"]) for job in grids) == count
    assert len(grids) == (count + 8) // 9
    assert [job["continuity_index"] for job in grids] == list(range(len(grids)))
    for previous, current in zip(grids, grids[1:], strict=False):
        assert current["continuity_previous_asset_id"] == previous["asset_id"]
        assert current["continuity_chain_id"] == previous["continuity_chain_id"]


def test_multi_grid_sequence_resumes_without_paid_calls(tmp_path):
    plan = _plan(10)
    calls = []

    class Direct:
        def generate_png(self, **kwargs):
            calls.append(kwargs)
            width, height = map(int, kwargs["size"].split("x"))
            buffer = BytesIO()
            Image.new("RGB", (width, height), (35 + len(calls), 40, 50)).save(
                buffer, format="PNG"
            )
            data = buffer.getvalue()
            kwargs["destination"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["destination"].write_bytes(data)
            return {
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": width, "height": height,
                "size": kwargs["size"], "usage": None,
            }

    client = Direct()
    kwargs = {
        "api_key": None, "options": ImageOptions(), "direct_client": client,
    }
    result = generate_b2_images(tmp_path, plan, **kwargs)
    assert len(calls) == 2
    assert result["status"] == "completed"
    assert "reference_image_urls" not in calls[0]
    again = generate_b2_images(tmp_path, plan, **kwargs)
    assert again["status"] == "completed"
    assert len(calls) == 2


@pytest.mark.parametrize("prompt", ["19. Panel nineteen.", "20. Panel twenty."])
def test_legacy_grid_parser_accepts_panels_nineteen_and_twenty(prompt):
    marker = parse_grid_marker(prompt)

    assert marker is not None
    assert marker.style == "legacy"
    assert marker.panel_number in {19, 20}


@pytest.mark.parametrize("prompt", ["A19. Panel nineteen.", "A20. Panel twenty."])
def test_lettered_grid_parser_accepts_panels_nineteen_and_twenty(prompt):
    marker = parse_grid_marker(prompt)

    assert marker is not None
    assert marker.style == "lettered"
    assert marker.panel_number in {19, 20}


def test_cta_avatar_media_uses_ebook_only_and_generated_video_cannot_speak():
    fields = {
        "beat_id": "1A", "visual_type": "avatar_media",
        "prompt": "A still supporting the original narrator.",
        "video_prompt": None, "audio": True, "reference": True,
        "reference_sources": ["ebook"],
    }
    assert ProfessionalCTAVisualBeat.model_validate(fields).audio
    with pytest.raises(ValidationError):
        ProfessionalCTAVisualBeat.model_validate({
            **fields, "reference_sources": ["presenter", "ebook"],
        })
    with pytest.raises(ValidationError):
        ProfessionalCTAVisualBeat.model_validate({
            **fields, "visual_type": "media_video", "audio": True,
            "video_prompt": "A person moves naturally.",
        })
    with pytest.raises(ValidationError):
        ProfessionalCTAVisualBeat.model_validate({
            **fields, "visual_type": "media_image", "audio": False,
            "reference_sources": ["presenter"],
        })


def test_cta_ebook_only_grid_does_not_claim_presenter_reference():
    plan = {
        "visual_style": "profesional",
        "blocks": [{
            "block_id": 1, "type": "development",
            "beats": [{
                "block_id": 1,
                "beat_id": f"1{chr(ord('A') + i)}",
                "visual_type": "avatar_media",
                "text": f"Book context {i}",
                "prompt": f"{i + 1}. A useful desk-side support image.",
                "video_prompt": None,
                "audio": True,
                "reference": True,
                "cta_group_id": 1,
                "cta_reference_sources": ["ebook"],
            } for i in range(2)],
        }],
    }
    jobs = plan_image_jobs(plan, described_beats(plan))
    assert len(jobs) == 1
    assert jobs[0]["grid"] is not None
    assert "ebook-cover reference" in jobs[0]["description"]
    assert "presenter identity reference" not in jobs[0]["description"]
