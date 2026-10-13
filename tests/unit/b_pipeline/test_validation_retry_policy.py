"""Regression tests for contextual retry budgets and global stage isolation."""
import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.contracts import (
    B2Output,
    B11Output,
    B12Output,
    WholeB12Output,
    WholeProfessionalB2Output,
)
from ai_video_factory.bots.workflow import BPipelineValidationError, run_b_pipeline

SOURCE = "First passage is distinct. Second passage is another separate thought."


def one_block_b11() -> B11Output:
    return B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Question?", "final_answer": "Answer."},
        "blocks": [{
            "block_id": 1, "type": "development",
            "emotional_entry": "Before", "emotional_exit": "After",
            "span": {"first_words": SOURCE, "last_words": SOURCE},
        }],
        "error": None,
    })


def valid_b11() -> B11Output:
    return B11Output.model_validate({
        "pipeline_stage": "B1.1",
        "narrative_core": {"central_question": "Question?", "final_answer": "Answer."},
        "blocks": [
            {
                "block_id": 1, "type": "development",
                "emotional_entry": "Before", "emotional_exit": "During",
                "span": {"first_words": "First passage", "last_words": "distinct."},
            },
            {
                "block_id": 2, "type": "development",
                "emotional_entry": "During", "emotional_exit": "After",
                "span": {
                    "first_words": "Second passage",
                    "last_words": "separate thought.",
                },
            },
        ],
        "error": None,
    })


class ContextualB11:
    supports_response_continuation = True

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = []

    async def generate_structured_with_response(self, **kwargs):
        self.calls.append(kwargs)
        attempt = len(self.calls)
        if attempt > 1:
            assert kwargs["previous_response_id"] == f"b11_{attempt - 1}"
            assert "VALIDATION FAILED: B1.1" in kwargs["input_text"]
            assert "anchor" in kwargs["input_text"].lower()
        bad = one_block_b11().model_dump()
        bad["blocks"][0]["span"]["first_words"] = "not in original script"
        output = B11Output.model_validate(bad) if attempt <= self.failures else valid_b11()
        return output, SimpleNamespace(id=f"b11_{attempt}")


def test_b11_continues_prior_response_twice_when_needed() -> None:
    provider = ContextualB11(failures=2)
    rejected = []
    result = asyncio.run(run_b_pipeline(
        SOURCE, provider=provider, stop_after="B1.1",
        on_rejected_output=lambda *args: rejected.append(args),
    ))
    assert len(provider.calls) == 3
    assert [item[0] for item in rejected] == [1, 2]
    assert len(result.b11.blocks) == 2


def test_b11_fails_after_exactly_two_contextual_retries() -> None:
    provider = ContextualB11(failures=3)
    rejected = []
    with pytest.raises(BPipelineValidationError, match="after 2 retries"):
        asyncio.run(run_b_pipeline(
            SOURCE, provider=provider, stop_after="B1.1",
            on_rejected_output=lambda *args: rejected.append(args),
        ))
    assert len(provider.calls) == 3
    assert [item[0] for item in rejected] == [1, 2, 3]


class OneInvalidB12:
    def __init__(self):
        self.calls = Counter()

    async def generate_structured(self, **kwargs):
        assert kwargs["output_type"] is WholeB12Output
        data = json.loads(kwargs["input_text"])
        self.calls["whole"] += 1
        return WholeB12Output.model_validate({
            "pipeline_stage": "B1.2",
            "narrative_core": data["narrative_core"],
            "blocks": [{
                "block_id": block["block_id"],
                "beats": [{
                    "beat_id": (
                        "1X" if block["block_id"] == 1 and self.calls["whole"] == 1
                        else f"{block['block_id']}A"
                    ),
                    "text": block["text"], "beat_type": "claim",
                }],
            } for block in data["blocks"]],
            "error": None,
        })


def test_b12_retries_the_complete_video_after_invalid_block() -> None:
    provider = OneInvalidB12()
    result = asyncio.run(run_b_pipeline(
        SOURCE, provider=provider,
        start_from="B1.2", stop_after="B1.2",
        previous_b11=valid_b11(),
    ))
    assert provider.calls == {"whole": 2}
    assert len(result.b12) == 2


class TwoBadB2:
    def __init__(self, output_type):
        self.output_type = output_type
        self.calls = Counter()

    async def generate_structured(self, **kwargs):
        data = json.loads(kwargs["input_text"])
        assert kwargs["output_type"] is self.output_type
        block = data["blocks"][0]
        block_id = block["block_id"]
        self.calls[block_id] += 1
        beat = block["beats"][0]
        if self.output_type is B2Output:
            beat.update(
                visual_type="media_image" if self.calls[block_id] <= 2 else "avatar",
                description=None,
            )
        else:
            beat.update(
                visual_type="media_image" if self.calls[block_id] <= 2 else "avatar",
                prompt=None, video_prompt=None, reference=False,
                audio=self.calls[block_id] > 2,
            )
        return self.output_type.model_validate({
            "pipeline_stage": "B2",
            "narrative_core": data["narrative_core"],
            "blocks": [block], "error": None,
        })


@pytest.mark.parametrize("model", [B2Output, WholeProfessionalB2Output])
def test_b2_styles_can_repair_a_schema_failure_twice(model) -> None:
    script = SOURCE
    b11 = one_block_b11()
    first = script[:len(script)//2]
    second = script[len(script)//2:]
    b12 = B12Output.model_validate({
        "pipeline_stage": "B1.2", "block_id": 1,
        "beats": [
            {"beat_id": "1A", "text": first, "beat_type": "claim"},
            {"beat_id": "1B", "text": second, "beat_type": "claim"},
        ],
        "error": None,
    })
    # Fail twice on beat 1; repair on the third attempt. Keep beat 2 valid.
    class BrokenThenFixed(TwoBadB2):
        async def generate_structured(self, **kwargs):
            data = json.loads(kwargs["input_text"])
            self.calls[1] += 1
            for index, beat in enumerate(data["blocks"][0]["beats"]):
                invalid = index == 0 and self.calls[1] <= 2
                if self.output_type is B2Output:
                    beat.update(
                        visual_type="media_image" if invalid else "avatar",
                        description=None,
                    )
                else:
                    beat.update(
                        visual_type="media_image" if invalid else "avatar",
                        prompt=None, video_prompt=None, reference=False,
                        audio=not invalid,
                    )
            return self.output_type.model_validate({
                "pipeline_stage": "B2", "narrative_core": data["narrative_core"],
                "blocks": data["blocks"], "error": None,
            })

    provider = BrokenThenFixed(model)
    result = asyncio.run(run_b_pipeline(
        script, provider=provider, visual_style=(
            "profesional" if model is WholeProfessionalB2Output else "casero"
        ), avatar_image=b"png" if model is WholeProfessionalB2Output else None,
        start_from="B2", previous_b11=b11, previous_b12=(b12,),
    ))
    assert provider.calls[1] == 3
    assert result.b2[0].blocks[0].beats[0].visual_type == "avatar"


def test_failed_whole_b12_never_persists_a_partial_segmentation() -> None:
    class AlwaysBad(OneInvalidB12):
        async def generate_structured(self, **kwargs):
            data = json.loads(kwargs["input_text"])
            self.calls["whole"] += 1
            return WholeB12Output.model_validate({
                "pipeline_stage": "B1.2",
                "narrative_core": data["narrative_core"],
                "blocks": [{
                    "block_id": block["block_id"],
                    "beats": [{
                        "beat_id": "1X" if block["block_id"] == 1 else "2A",
                        "text": block["text"], "beat_type": "claim",
                    }],
                } for block in data["blocks"]],
                "error": None,
            })

    provider = AlwaysBad()
    saved = {}
    with pytest.raises(BPipelineValidationError, match="after 2 targeted retries each"):
        asyncio.run(run_b_pipeline(
            SOURCE, provider=provider, start_from="B1.2", stop_after="B1.2",
            previous_b11=valid_b11(),
            on_output=lambda stage, block_id, payload: saved.update({
                (stage, block_id): payload,
            }),
        ))
    assert provider.calls == {"whole": 3}
    assert saved == {}
    recovering = OneInvalidB12()
    complete = asyncio.run(run_b_pipeline(
        SOURCE, provider=recovering, start_from="B1.2", stop_after="B1.2",
        previous_b11=valid_b11(), previous_partial_b12={},
    ))
    assert len(complete.b12) == 2
    assert recovering.calls == {"whole": 2}
