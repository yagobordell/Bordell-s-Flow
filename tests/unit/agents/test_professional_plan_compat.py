"""Verify old B2 is strict-compatible while 3–4 image plans remain renderable."""
from __future__ import annotations

import pytest

from ai_video_factory.agents.professional.compat import project_professional_plan
from ai_video_factory.agents.professional.prompt_loader import (
    load_phase_instructions, load_post_generation_motion_instructions,
)
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats
def sample():
    parts = ["Una idea concreta. ", "Otra idea clara. ", "Una tercera prueba. ", "Y cerramos."]
    script = "".join(parts)
    groups = [{
        "block_id": 1, "type": "close",
        "emotional_entry": "neutro", "emotional_exit": "conclusion",
        "span": {"first_words": "Una idea concreta.", "last_words": "Y cerramos."},
    }]
    beats = [
        {
            "beat_id": f"1{chr(65 + i)}", "text": part,
            "beat_type": "claim",
            "visual_type": "media_image", "prompt": None, "video_prompt": None,
            "audio": False, "reference": False,
            "image_prompts": [f"Meaningful image {i + 1}"],
            "generation_mode": "independent",
        }
        for i, part in enumerate(parts)
    ]
    beats[-1] = {
        "beat_id": "1D", "text": parts[-1], "beat_type": "return",
        "visual_type": "avatar", "prompt": None, "video_prompt": None,
        "audio": True, "reference": False,
    }
    plan = {
        "visual_style": "profesional",
        "narrative_core": {"central_question": "¿Qué?", "final_answer": "Una conclusión."},
        "blocks": [{"block_id": 1, "beats": beats}],
    }
    return script, groups, plan



@pytest.mark.parametrize("count", [2, 3, 4])
def test_project_multi_image_beat_and_strict_voice(count):
    script, groups, plan = sample()
    beat = plan["blocks"][0]["beats"][0]
    beat["image_prompts"] = [f"A different viewpoint {i}" for i in range(count)]
    beat["generation_mode"] = "grid"
    beat["editor_notes"] = [{
        "kind": "number_overlay",
        "trigger": {"text": "Una", "occurrence": 1},
    }]
    output = project_professional_plan(script, groups, plan)
    generated = output["visual_plan"]["blocks"][0]["beats"][0]
    assert len(generated["visual_subshots"]) == count
    assert generated["visual_subshots"][-1]["subshot_count"] == count
    assert generated["visual_subshots"][-1]["asset_id"] == f"1A__{count}"
    assert "editor_notes" not in generated
    strict_beat = output["b2_merged"]["blocks"][0]["beats"][0]
    assert "visual_subshots" not in strict_beat
    assert "editor_notes" not in strict_beat
    assert "image_prompts" not in strict_beat
    assert len(output["editor_plan"]["blocks"][0]["beats"][0]["notes"]) == 1
    assets = described_beats(output["visual_plan"])
    assert len([x for x in assets if x["beat_id"] == "1A"]) == count
    jobs = plan_image_jobs(output["visual_plan"], assets)
    assert jobs


def test_single_image_beat_has_no_extra_asset_fields():
    script, groups, plan = sample()
    output = project_professional_plan(script, groups, plan)
    beat = output["visual_plan"]["blocks"][0]["beats"][0]
    assert beat["prompt"] == "Meaningful image 1"
    assert "visual_subshots" not in beat


def test_prompt_loader_includes_unabridged_screen_rule_every_editing_phase():
    for phase in ("02_planning", "03_evaluation", "04_correction"):
        instructions = load_phase_instructions(phase)
        assert "(For context: ...)" in instructions
        assert "content shown within the screen or document" in instructions
        assert "B2.1" in instructions
    assert "same logical agent" in load_phase_instructions("01_groups").lower() or (
        "ONE creative editor/director" in load_phase_instructions("01_groups")
    )
    assert "actual finalized start PNGs" in load_post_generation_motion_instructions()


def test_invalid_phase_fails_closed():
    with pytest.raises(ValueError, match="Unsupported"):
        load_phase_instructions("casero")
