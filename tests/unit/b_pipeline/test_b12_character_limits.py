"""B1.2 beat-length limits and targeted block correction."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.contracts import (
    B11Output,
    B12Output,
    Beat,
    MaterializedBlock,
    WholeB12Output,
)
from ai_video_factory.bots.workflow import (
    BPipelineValidationError,
    run_b_pipeline,
    validate_beats,
)
from scripts.pipeline.run_b_pipeline import BotArtifacts

FIRST = "La primera descripción explica un detalle concreto y permite comprender el contexto. "
SECOND = "La segunda descripción aporta un dato distinto y conserva el orden original."
SHORT = " No lo sé. "


def _block(text: str) -> MaterializedBlock:
    return MaterializedBlock(
        block_id=1,
        type="development",
        emotional_entry="inicio",
        emotional_exit="fin",
        text=text,
    )


def _output(*texts: str) -> B12Output:
    return B12Output(
        pipeline_stage="B1.2",
        block_id=1,
        beats=[
            Beat(beat_id=f"1{chr(ord('A') + i)}", text=text, beat_type="claim")
            for i, text in enumerate(texts)
        ],
        error=None,
    )


def _b11(script: str) -> B11Output:
    return B11Output.model_validate(
        {
            "pipeline_stage": "B1.1",
            "narrative_core": {"central_question": "¿Qué sucede?", "final_answer": "Dos hechos."},
            "blocks": [
                {
                    "block_id": 1,
                    "type": "development",
                    "emotional_entry": "inicio",
                    "emotional_exit": "fin",
                    "span": {
                        "first_words": " ".join(script.split()[:2]),
                        "last_words": " ".join(script.split()[-2:]),
                    },
                }
            ],
            "error": None,
        }
    )


class RepairProvider:
    def __init__(self, *responses: B12Output) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def generate_structured_with_response(self, **kwargs):
        assert kwargs["output_type"] is WholeB12Output
        assert kwargs["model"] == "test-model"
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        payload = json.loads(kwargs["input_text"])
        return WholeB12Output.model_validate({
            "pipeline_stage": "B1.2", "narrative_core": payload["narrative_core"],
            "blocks": [{"block_id": 1, "beats": [beat.model_dump() for beat in result.beats]}],
            "error": None,
        }), SimpleNamespace(id=f"response_{len(self.calls)}")


@pytest.mark.parametrize(
    ("length", "valid"),
    [(1, True), (12, True), (13, True), (100, True), (119, True), (120, True), (121, False)],
)
def test_beat_character_boundaries(length: int, valid: bool) -> None:
    first = "x" * length + " "
    remaining = "La continuación explica otro hecho completo."
    block = _block(first + remaining)
    output = _output(first, remaining)
    if valid:
        assert validate_beats(output, block) == output.beats
    else:
        with pytest.raises(BPipelineValidationError, match="characters"):
            validate_beats(output, block)


def test_character_count_ignores_only_boundary_whitespace() -> None:
    first = " " * 3 + "x" * 120 + "\n\t"
    remaining = "La continuación explica otro hecho completo."
    assert validate_beats(_output(first, remaining), _block(first + remaining))


@pytest.mark.parametrize(
    "block_type", ["intro", "development", "pivot", "correction", "climax", "close"]
)
def test_short_beats_are_valid_in_every_block_type(block_type: str) -> None:
    text = "A b Una frase breve y completa."
    block = MaterializedBlock(
        block_id=1, type=block_type,
        emotional_entry="inicio", emotional_exit="fin", text=text,
    )
    beats = validate_beats(_output("A ", "b ", "Una frase breve y completa."), block)
    assert [beat.text.strip() for beat in beats] == ["A", "b", "Una frase breve y completa."]


def test_short_sole_beat_of_a_frozen_block_is_valid() -> None:
    assert validate_beats(_output("  Sí.  "), _block("  Sí.  "))


def test_short_beat_in_multibeat_block_is_not_rejected_by_character_count() -> None:
    assert len(SHORT.strip()) <= 12
    assert validate_beats(
        _output(FIRST, SHORT, SECOND), _block(FIRST + SHORT + SECOND)
    )


def test_overlong_beat_is_repaired_once_before_saving_b12_and_starting_b2() -> None:
    script = FIRST + SECOND
    assert len(script.strip()) > 120
    provider = RepairProvider(_output(script), _output(FIRST, SECOND))
    rejected = []
    metered = []
    saved = []
    stages = []
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            model="test-model",
            start_from="B1.2",
            stop_after="B1.2",
            previous_b11=_b11(script),
            on_rejected_b12_output=lambda *args: rejected.append(args),
            on_api_response=lambda *args: metered.append(args),
            on_output=lambda *args: saved.append(args),
            on_stage_complete=lambda *args: stages.append(args),
        )
    )
    assert len(provider.calls) == len(metered) == 2
    assert [call[0] for call in rejected] == [0]
    assert [call[1] for call in rejected] == [1]
    assert "maximum 120" in rejected[0][2]
    assert "Correct only source block groups: 1." in provider.calls[1]["instructions"]
    assert "at or below 40 words and 120 stripped" in provider.calls[1]["instructions"]
    assert "no minimum character count" in provider.calls[1]["instructions"]
    assert "13 characters" not in provider.calls[1]["instructions"]
    assert all(json.loads(call["input_text"])["blocks"][0]["text"] == script
               for call in provider.calls)
    assert len(saved) == 2 and all(item[0] == "B1.2" for item in saved)
    assert len(stages) == 1 and stages[0][0] == "B1.2"
    assert result.b2 == ()
    assert [beat.beat_id for beat in result.b12[0].beats] == ["1A", "1B"]
    assert "".join(beat.text for beat in result.b12[0].beats) == script


def test_short_beat_does_not_trigger_character_repair() -> None:
    script = FIRST + SHORT + SECOND
    provider = RepairProvider(_output(FIRST, SHORT, SECOND))
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            model="test-model",
            start_from="B1.2",
            stop_after="B1.2",
            previous_b11=_b11(script),
        )
    )
    assert len(provider.calls) == 1
    assert "".join(beat.text for beat in result.b12[0].beats) == script
    assert [beat.text.strip() for beat in result.b12[0].beats] == [
        FIRST.strip(), SHORT.strip(), SECOND.strip(),
    ]
    assert all(len(beat.text.strip()) <= 120 for beat in result.b12[0].beats)


def test_second_invalid_repair_fails_closed_with_both_rejections() -> None:
    script = FIRST + SECOND
    provider = RepairProvider(_output(script), _output(script), _output(script))
    rejected = []
    saved = []
    with pytest.raises(BPipelineValidationError, match="after 2 targeted retries each"):
        asyncio.run(
            run_b_pipeline(
                script,
                provider=provider,
                model="test-model",
                start_from="B1.2",
                stop_after="B1.2",
                previous_b11=_b11(script),
                on_rejected_b12_output=lambda *args: rejected.append(args),
                on_output=lambda *args: saved.append(args),
            )
        )
    assert len(provider.calls) == 3
    assert [item[1] for item in rejected] == [1, 2, 3]
    assert saved == []


def test_repaired_text_restores_source_punctuation_without_extra_retry() -> None:
    script = FIRST + SECOND
    altered_second = SECOND[:-1] + ","
    provider = RepairProvider(_output(script), _output(FIRST, altered_second))
    rejected = []
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            model="test-model",
            start_from="B1.2",
            stop_after="B1.2",
            previous_b11=_b11(script),
            on_rejected_b12_output=lambda *args: rejected.append(args),
        )
    )
    # Model-selected content and beat boundaries are preserved while source
    # punctuation is restored by the canonicalizer, not sent for another retry.
    assert len(provider.calls) == 2
    assert [item[1] for item in rejected] == [1]
    assert [beat.beat_id for beat in result.b12[0].beats] == ["1A", "1B"]
    assert "".join(beat.text for beat in result.b12[0].beats) == script
    assert result.b12[0].beats[-1].text.endswith(".")


def test_correction_may_preserve_valid_short_beats() -> None:
    script = FIRST + SHORT + SECOND
    provider = RepairProvider(_output(script), _output(FIRST, SHORT, SECOND))
    result = asyncio.run(
        run_b_pipeline(
            script,
            provider=provider,
            model="test-model",
            start_from="B1.2",
            stop_after="B1.2",
            previous_b11=_b11(script),
        )
    )
    assert len(provider.calls) == 2
    assert "".join(beat.text for beat in result.b12[0].beats) == script
    assert [beat.text.strip() for beat in result.b12[0].beats] == [
        FIRST.strip(), SHORT.strip(), SECOND.strip(),
    ]
    assert "no minimum character count" in provider.calls[1]["instructions"]


def test_unrelated_b12_validation_failure_is_repaired() -> None:
    script = FIRST + SECOND
    provider = RepairProvider(
        _output(FIRST.replace("primera", "tercera"), SECOND),
        _output(FIRST, SECOND),
    )
    result = asyncio.run(
        run_b_pipeline(
                script,
                provider=provider,
                model="test-model",
                start_from="B1.2",
                stop_after="B1.2",
                previous_b11=_b11(script),
        )
    )
    assert len(provider.calls) == 2
    assert "".join(beat.text for beat in result.b12[0].beats) == script


def test_rejected_b12_attempt_is_persisted(tmp_path) -> None:
    BotArtifacts(tmp_path).rejected_b12(3, 1, "too short", {"beats": []})
    saved = json.loads(
        (tmp_path / "B1.2" / "block_3" / "rejected_attempt_1.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved == {
        "attempt": 1,
        "validation_error": "too short",
        "response": {"beats": []},
    }
