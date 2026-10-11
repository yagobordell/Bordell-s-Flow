"""Cross-carrier numbered grids and beat-owned editor text."""

import pytest

from ai_video_factory.agents.professional.compact import (
    SparseDirection,
    materialize_sparse_direction,
)
from ai_video_factory.providers.image_grids import GridPlanError, plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats


def _beat(block_id, suffix, carrier, prompt=None, **extra):
    beat = {
        "beat_id": f"{block_id}{suffix}", "visual_type": carrier,
        "text": f"Scene {block_id}{suffix}.", "prompt": prompt,
        "video_prompt": "The object moves." if carrier == "media_video" else None,
        "audio": carrier in {"avatar", "avatar_media"}, "reference": False,
    }
    beat.update(extra)
    return beat


def _plan(blocks):
    return {
        "visual_style": "profesional",
        "blocks": [
            {"block_id": index, "type": "development", "beats": beats}
            for index, beats in enumerate(blocks, start=1)
        ],
    }


def test_grid_joins_video_still_avatar_media_across_avatar_and_groups():
    plan = _plan([
        [
            _beat(1, "A", "media_video", "A1. A person reaches toward a tabletop."),
            _beat(1, "B", "avatar"),
            _beat(1, "C", "media_image", "A2. Close detail of the same tabletop."),
        ],
        [
            _beat(2, "A", "avatar_media", "A3. Supporting still in the same room."),
            _beat(2, "B", "media_video", "A4. The person prepares to stand."),
        ],
    ])
    jobs = plan_image_jobs(plan, described_beats(plan))
    assert len(jobs) == 1
    grid = jobs[0]
    assert grid["grid_id"] == "A"
    assert grid["grid"]["grid_asset_labels"] == ["A1", "A2", "A3", "A4"]
    assert grid["grid"]["numbered_beat_ids"] == ["1A", "1C", "2A", "2B"]
    assert [panel["visual_type"] for panel in grid["panels"]] == [
        "media_video", "media_image", "avatar_media", "media_video",
    ]


def test_five_asset_grid_with_four_still_subshots_still_packs():
    subshots = [
        {
            "asset_id": f"1A__{index}", "subshot_index": index,
            "subshot_count": 4, "generation": "grid",
            "prompt": f"A{index}. Static composition {index}.",
        }
        for index in range(1, 5)
    ]
    plan = _plan([[
        _beat(1, "A", "media_image", "A1. Static composition 1.",
              visual_subshots=subshots),
        _beat(1, "B", "media_video", "A5. The character starts moving."),
    ]])
    grids = [job for job in plan_image_jobs(plan, described_beats(plan)) if job.get("grid")]
    assert [len(job["grid"]["numbered_asset_ids"]) for job in grids] == [3, 2]
    assert [job["continuity_chain_id"] for job in grids] == ["grid:A", "grid:A"]
    assert grids[1]["continuity_previous_asset_id"] == grids[0]["asset_id"]


def test_single_asset_labeled_grid_is_invalid():
    plan = _plan([[
        _beat(1, "A", "media_video", "A1. A continuous movement begins."),
        _beat(1, "B", "avatar"),
    ]])
    with pytest.raises(GridPlanError, match="2"):
        plan_image_jobs(plan, described_beats(plan))


def test_nonsequential_grid_number_is_invalid():
    plan = _plan([[
        _beat(1, "A", "media_video", "A1. A moving frame."),
        _beat(1, "B", "media_video", "A3. A later moving frame."),
    ]])
    with pytest.raises(GridPlanError, match="expected A2"):
        plan_image_jobs(plan, described_beats(plan))


def test_sparse_direction_accepts_string_editor_notes_and_optional_grid_mode():
    first = "The telephone vibrates. "
    second = "The number is visible."
    direction = SparseDirection.model_validate({
        "blocks": [{
            "block_id": 1,
            "beats": [
                {
                    "beat_id": "1A", "text": first, "beat_type": "action",
                    "visual_type": "media_video",
                    "prompt": "A1. A telephone resting on a desk.",
                    "video_prompt": "The phone vibrates gently on the desk.",
                    "editor_notes": "",
                },
                {
                    "beat_id": "1B", "text": second, "beat_type": "evidence",
                    "visual_type": "media_image",
                    "image_prompts": ["A2. Fixed close-up of the same phone."],
                    "editor_notes": "Añade un titular centrado sobre el plano.",
                },
            ],
            "presenter_references": [], "cta": [],
        }],
    })
    result = materialize_sparse_direction(
        [{"block_id": 1, "text": first + second}],
        direction,
        {"central_question": "What is displayed?", "final_answer": "A number."},
    )
    assert result["status"] == "passed", result["issues"]
    beats = result["plan"]["blocks"][0]["beats"]
    assert beats[0]["prompt"].startswith("A1.")
    assert beats[0]["editor_notes"] == ""
    assert beats[1]["image_prompts"] == ["A2. Fixed close-up of the same phone."]
    assert beats[1]["editor_notes"] == "Añade un titular centrado sobre el plano."
