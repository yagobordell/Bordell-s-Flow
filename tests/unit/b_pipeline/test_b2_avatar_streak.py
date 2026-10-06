"""Hard 120-character narration-run checks for both B2 visual styles."""

import asyncio
import json

import pytest

from ai_video_factory.bots.contracts import (
    B2Input,
    B2Output,
    B11Output,
    B12Output,
    ProfessionalB2Output,
)
from ai_video_factory.bots.workflow import (
    BPipelineValidationError,
    _overlong_avatar_run,
    run_b_pipeline,
    validate_visuals,
)


def _b2_pair(
    lengths: list[int],
    visuals: list[str],
    *,
    professional: bool = False,
    audios: list[bool] | None = None,
) -> tuple[B2Input, B2Output | ProfessionalB2Output]:
    source = B2Input.model_validate(
        {
            "narrative_core": {"central_question": "¿Qué sucede?", "final_answer": "Esto."},
            "current_block_id": 1,
            "blocks": [
                {
                    "block_id": 1,
                    "type": "development",
                    "emotional_entry": "Inicio",
                    "emotional_exit": "Final",
                    "span": {"first_words": "Inicio", "last_words": "Final"},
                    "beats": [
                        {
                            "beat_id": f"1{chr(ord('A') + index)}",
                            "text": chr(ord("A") + index) * length,
                            "beat_type": "claim",
                        }
                        for index, length in enumerate(lengths)
                    ],
                }
            ],
        }
    )
    block = source.blocks[0].model_dump()
    for index, (beat, visual) in enumerate(zip(block["beats"], visuals, strict=True)):
        beat["visual_type"] = visual
        if professional:
            audio = (
                audios[index]
                if audios is not None
                else visual in {"avatar", "avatar_media"}
            )
            beat.update(
                prompt=None if visual == "avatar" else "Faithful source-grounded image.",
                video_prompt=(
                    "One natural continuous motion from the reference frame."
                    if visual == "media_video" else None
                ),
                reference=visual == "media_video" and audio,
                audio=audio,
            )
        else:
            beat["description"] = None if visual == "avatar" else "Faithful scene."
    output_type = ProfessionalB2Output if professional else B2Output
    output = output_type.model_validate(
        {
            "pipeline_stage": "B2",
            "narrative_core": source.narrative_core.model_dump(),
            "blocks": [block],
            "error": None,
        }
    )
    return source, output


@pytest.mark.parametrize("professional", [False, True])
def test_avatar_run_accepts_exactly_120_characters(professional: bool) -> None:
    supplied, result = _b2_pair(
        [60, 60], ["avatar", "avatar_media"], professional=professional
    )
    validate_visuals(result, supplied)


@pytest.mark.parametrize("professional", [False, True])
def test_avatar_run_rejects_121_characters_in_both_styles(professional: bool) -> None:
    supplied, result = _b2_pair(
        [60, 61], ["avatar", "avatar_media"], professional=professional
    )
    with pytest.raises(BPipelineValidationError, match="total 121 characters"):
        validate_visuals(result, supplied)


def test_non_presenter_visual_resets_run() -> None:
    supplied, result = _b2_pair(
        [90, 13, 90], ["avatar", "media_image", "avatar_media"]
    )
    validate_visuals(result, supplied)


def test_professional_media_video_audio_true_extends_120_character_run() -> None:
    supplied, result = _b2_pair(
        [60, 61],
        ["avatar", "media_video"],
        professional=True,
        audios=[True, True],
    )
    with pytest.raises(
        BPipelineValidationError,
        match=r"consecutive audio=true beats .* total 121 characters",
    ):
        validate_visuals(result, supplied)


def test_professional_audio_false_resets_run_even_for_media_video() -> None:
    supplied, result = _b2_pair(
        [90, 13, 90],
        ["avatar", "media_video", "avatar_media"],
        professional=True,
        audios=[True, False, True],
    )
    validate_visuals(result, supplied)


def test_professional_visual_type_change_does_not_reset_spoken_run() -> None:
    supplied, result = _b2_pair(
        [40, 40, 41],
        ["avatar", "avatar_media", "media_video"],
        professional=True,
        audios=[True, True, True],
    )
    with pytest.raises(BPipelineValidationError, match="total 121 characters"):
        validate_visuals(result, supplied)


def _checkpoint(
    first: str, second_beats: list[str]
) -> tuple[str, B11Output, tuple[B12Output, B12Output]]:
    # Block 1 owns its separator; block 2 owns its internal beat separator.
    script = first + " " + " ".join(second_beats)
    b11 = B11Output.model_validate(
        {
            "pipeline_stage": "B1.1",
            "narrative_core": {"central_question": "¿Qué sucede?", "final_answer": "Esto."},
            "blocks": [
                {
                    "block_id": 1,
                    "type": "intro",
                    "emotional_entry": "Inicio",
                    "emotional_exit": "Medio",
                    "span": {"first_words": first, "last_words": first},
                },
                {
                    "block_id": 2,
                    "type": "close",
                    "emotional_entry": "Medio",
                    "emotional_exit": "Final",
                    "span": {
                        "first_words": second_beats[0],
                        "last_words": second_beats[-1],
                    },
                },
            ],
            "error": None,
        }
    )
    b12 = (
        B12Output.model_validate(
            {
                "pipeline_stage": "B1.2",
                "block_id": 1,
                "beats": [
                    {"beat_id": "1A", "text": first + " ", "beat_type": "claim"}
                ],
                "error": None,
            }
        ),
        B12Output.model_validate(
            {
                "pipeline_stage": "B1.2",
                "block_id": 2,
                "beats": [
                    {
                        "beat_id": f"2{chr(ord('A') + index)}",
                        "text": text + (" " if index < len(second_beats) - 1 else ""),
                        "beat_type": "claim",
                    }
                    for index, text in enumerate(second_beats)
                ],
                "error": None,
            }
        ),
    )
    return script, b11, b12


class _B2Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def generate_structured(
        self, *, model: str, instructions: str, input_text: str, output_type: type
    ) -> B2Output:
        assert output_type is B2Output
        data = json.loads(input_text)
        block = data["blocks"][0]
        self.calls.append((block["block_id"], instructions))
        for beat in block["beats"]:
            repair = (
                "whole-video avatar-run correction" in instructions
                and beat["beat_id"] == "2A"
            )
            beat["visual_type"] = "media_image" if repair else "avatar"
            beat["description"] = "Faithful source-grounded visual." if repair else None
        return B2Output.model_validate(
            {
                "pipeline_stage": "B2",
                "narrative_core": data["narrative_core"],
                "blocks": [block],
                "error": None,
            }
        )


def test_whole_video_retries_only_discretionary_beat_in_adjacent_block() -> None:
    script, b11, b12 = _checkpoint("A" * 61, ["B" * 60, "C" * 13])
    provider = _B2Provider()
    rejected: list[tuple[int, str]] = []
    completed: list[str] = []
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            start_from="B2",
            previous_b11=b11,
            previous_b12=b12,
            on_rejected_b2_output=lambda block_id, _attempt, error, _output: rejected.append(
                (block_id, error)
            ),
            on_stage_complete=lambda stage, _count, _payload: completed.append(stage),
        )
    )
    assert [block_id for block_id, _ in provider.calls] == [1, 2, 2] or sorted(
        block_id for block_id, _ in provider.calls
    ) == [1, 2, 2]
    assert rejected[0][0] == 2
    assert "total 134 characters" in rejected[0][1]
    assert [beat.visual_type for beat in result.b2[1].blocks[0].beats] == [
        "media_image",
        "avatar",
    ]
    assert _overlong_avatar_run(result.b2) is None
    assert completed == ["B2"]


def test_cross_block_intro_close_beats_can_be_reassigned_to_break_avatar_run() -> None:
    script, b11, b12 = _checkpoint("A" * 61, ["B" * 60])
    provider = _B2Provider()
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            start_from="B2",
            previous_b11=b11,
            previous_b12=b12,
        )
    )
    assert result.b2[0].blocks[0].beats[0].visual_type == "avatar"
    assert result.b2[1].blocks[0].beats[0].visual_type == "media_image"
    assert _overlong_avatar_run(result.b2) is None
