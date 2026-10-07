"""Regression coverage for the general B2.1 whole-video visual review layer."""

import asyncio
import json
from copy import deepcopy
from importlib.resources import files

import pytest

from ai_video_factory.bots.contracts import (
    B11Output,
    B12Output,
    B21Output,
    ProfessionalB2Output,
)
from ai_video_factory.bots.workflow import (
    BPipelineValidationError,
    _b21_split_candidates,
    apply_b21_review,
    run_b_pipeline,
    validate_b21,
)
from ai_video_factory.providers.image_grids import plan_image_jobs
from ai_video_factory.providers.openai_batch_images import described_beats


def _beat(
    beat_id: str,
    text: str,
    prompt: str,
) -> dict[str, object]:
    return {
        "beat_id": beat_id,
        "text": text,
        "beat_type": "claim",
        "visual_type": "media_image",
        "prompt": prompt,
        "video_prompt": None,
        "reference": False,
        "audio": False,
    }


def _plan(beats: list[dict[str, object]]) -> dict[str, object]:
    return {
        "visual_style": "profesional",
        "blocks": [{
            "block_id": 1,
            "type": "development",
            "emotional_entry": "A",
            "emotional_exit": "B",
            "span": {"first_words": "A", "last_words": "B"},
            "beats": beats,
        }],
    }


def _review(*patches: dict[str, object]) -> B21Output:
    return B21Output.model_validate({
        "pipeline_stage": "B2.1",
        "modified_beats": list(patches),
        "error": None,
    })


def test_split_candidates_are_marked_by_exact_code_count() -> None:
    plan = _plan([
        _beat("1A", "x" * 49, "First image."),
        _beat("1B", "x" * 49 + " ", "Second image."),
        _beat("1C", "x" * 50, "Third image."),
    ])

    candidates = _b21_split_candidates(plan)

    assert [(item["beat_id"], item["character_count"]) for item in candidates] == [
        ("1B", 50),
        ("1C", 50),
    ]


def test_numbered_grid_split_inserts_panel_and_shifts_following_numbers() -> None:
    source = _plan([
        _beat("1A", "Short.", "1. Same subject in a wide shot."),
        _beat("1B", "This narration is deliberately longer than fifty characters total.",
              "2. Same subject in a medium shot."),
        _beat("1C", "Short again.", "3. Same subject in a close shot."),
    ])
    original = deepcopy(source)
    candidates = _b21_split_candidates(source)
    review = _review({
        "block_id": 1,
        "beat_id": "1B",
        "prompt": "Same subject in a medium shot.",
        "video_prompt": None,
        "second_prompt": "Same subject from a slightly lower camera angle.",
        "split_mode": "grid",
    })

    validate_b21(review, source_plan=source, split_candidates=candidates)
    reviewed = apply_b21_review(source, review)

    assert source == original
    beats = reviewed["blocks"][0]["beats"]
    assert beats[0]["prompt"].startswith("1. ")
    assert [item["prompt"].split(".", 1)[0] for item in beats[1]["visual_subshots"]] == [
        "2", "3",
    ]
    assert beats[2]["prompt"].startswith("4. ")

    expanded = described_beats(reviewed)
    assert [item["asset_id"] for item in expanded] == [
        "1A", "1B__1", "1B__2", "1C",
    ]
    jobs = plan_image_jobs(reviewed, expanded)
    assert len(jobs) == 1
    assert jobs[0]["grid"]["numbered_asset_ids"] == [
        "1A", "1B__1", "1B__2", "1C",
    ]


def test_unnumbered_split_can_choose_two_independent_generations() -> None:
    source = _plan([
        _beat(
            "1A",
            "This static beat is long enough that two visual subshots are required.",
            "A subject stands beside a window in a medium shot.",
        ),
    ])
    candidates = _b21_split_candidates(source)
    review = _review({
        "block_id": 1,
        "beat_id": "1A",
        "prompt": "A subject stands beside a window in a medium shot.",
        "video_prompt": None,
        "second_prompt": "The same subject beside the same window from a side angle.",
        "split_mode": "independent",
    })

    validate_b21(review, source_plan=source, split_candidates=candidates)
    reviewed = apply_b21_review(source, review)
    expanded = described_beats(reviewed)
    jobs = plan_image_jobs(reviewed, expanded)

    assert [item["asset_id"] for item in expanded] == ["1A__1", "1A__2"]
    assert [item["generation"] for item in expanded] == ["independent", "independent"]
    assert len(jobs) == 2
    assert all(job.get("grid") is None for job in jobs)


def test_split_variants_may_be_semantically_near_identical() -> None:
    # B2.1 is allowed to keep the same subject/action and vary only viewpoint.
    review = _review({
        "block_id": 1,
        "beat_id": "1A",
        "prompt": "Medium shot of the same person reading at the desk.",
        "video_prompt": None,
        "second_prompt": "Three-quarter medium shot of the same person reading at the desk.",
        "split_mode": "independent",
    })

    assert review.modified_beats[0].second_prompt is not None


def test_b21_prompt_allows_camera_only_difference_between_split_variants() -> None:
    prompt = (
        files("ai_video_factory.bots.prompts")
        .joinpath("b2_1_professional.md")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "be near-identical variants" in prompt
    assert "differ only in framing or camera angle" in prompt
    assert "same framing and the same camera angle" in prompt
    assert (
        "do not invent a semantic difference merely to make the images different."
        in prompt
    )


def test_numbered_split_cannot_escape_its_grid() -> None:
    source = _plan([
        _beat("1A", "Short.", "1. First view."),
        _beat(
            "1B",
            "This second numbered image has well over fifty narration characters.",
            "2. Second view.",
        ),
    ])
    candidates = _b21_split_candidates(source)
    review = _review({
        "block_id": 1,
        "beat_id": "1B",
        "prompt": "Second view.",
        "video_prompt": None,
        "second_prompt": "Second view from another angle.",
        "split_mode": "independent",
    })

    with pytest.raises(BPipelineValidationError, match="must stay in its grid"):
        validate_b21(review, source_plan=source, split_candidates=candidates)



def test_b21_receives_complete_merged_b2_and_code_marked_candidates() -> None:
    first = "The first visual sentence is deliberately long enough for two images. "
    second = "The second block also carries enough narration for two still images."
    script = first + second
    b11 = B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Q?", "final_answer": "A."},
        "blocks": [
            {
                "block_id": 1,
                "type": "development",
                "emotional_entry": "A",
                "emotional_exit": "B",
                "span": {"first_words": first.strip(), "last_words": first.strip()},
            },
            {
                "block_id": 2,
                "type": "development",
                "emotional_entry": "B",
                "emotional_exit": "C",
                "span": {"first_words": second, "last_words": second},
            },
        ],
        "error": None,
    })
    b12 = (
        B12Output.model_validate({
            "pipeline_stage": "B1.2",
            "block_id": 1,
            "beats": [{
                "beat_id": "1A",
                "text": first,
                "beat_type": "claim",
            }],
            "error": None,
        }),
        B12Output.model_validate({
            "pipeline_stage": "B1.2",
            "block_id": 2,
            "beats": [{
                "beat_id": "2A",
                "text": second,
                "beat_type": "claim",
            }],
            "error": None,
        }),
    )
    b21_inputs: list[dict[str, object]] = []
    b21_image_urls: list[str | None] = []

    class Provider:
        async def generate_structured(
            self, *, model, instructions, input_text, output_type,
            input_image_url=None,
        ):
            payload = json.loads(input_text)
            if output_type is ProfessionalB2Output:
                block = payload["blocks"][0]
                for beat in block["beats"]:
                    beat.update(
                        visual_type="media_image",
                        prompt=f"Visual treatment for {beat['beat_id']}.",
                        video_prompt=None,
                        reference=False,
                        audio=False,
                    )
                return ProfessionalB2Output.model_validate({
                    "pipeline_stage": "B2",
                    "narrative_core": payload["narrative_core"],
                    "blocks": [block],
                    "error": None,
                })
            if output_type is B21Output:
                b21_inputs.append(payload)
                b21_image_urls.append(input_image_url)
                return B21Output.model_validate({
                    "pipeline_stage": "B2.1",
                    "modified_beats": [
                        {
                            "block_id": item["block_id"],
                            "beat_id": item["beat_id"],
                            "prompt": (
                                f"Primary camera view for {item['beat_id']}."
                            ),
                            "video_prompt": None,
                            "second_prompt": (
                                f"Alternate camera angle for {item['beat_id']}."
                            ),
                            "split_mode": "independent",
                        }
                        for item in payload["b21_review"]["split_candidates"]
                    ],
                    "error": None,
                })
            raise RuntimeError(f"Unexpected output type: {output_type}")

    result = asyncio.run(run_b_pipeline(
        script,
        provider=Provider(),
        start_from="B2",
        stop_after="B2.1",
        previous_b11=b11,
        previous_b12=b12,
        visual_style="profesional",
        avatar_image=b"avatar",
    ))

    assert len(b21_inputs) == 1
    assert len(b21_image_urls) == 1
    assert isinstance(b21_image_urls[0], str)
    assert b21_image_urls[0].startswith("data:image/png;base64,")
    payload = b21_inputs[0]
    assert [block["block_id"] for block in payload["blocks"]] == [1, 2]
    assert [
        item["beat_id"] for item in payload["b21_review"]["split_candidates"]
    ] == ["1A", "2A"]
    assert result.b21 is not None
    assert [
        beat["visual_subshots"][1]["asset_id"]
        for block in result.visual_plan()["blocks"]
        for beat in block["beats"]
    ] == ["1A__2", "2A__2"]
