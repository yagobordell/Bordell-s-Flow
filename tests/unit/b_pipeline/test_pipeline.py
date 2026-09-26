import asyncio
import json

import pytest

from ai_video_factory.bots.contracts import (
    B2Input,
    B2Output,
    B11Output,
    B12Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
)
from ai_video_factory.bots.workflow import (
    PROMPT_CHECKSUMS,
    BPipelineValidationError,
    _beat_suffix,
    _instructions,
    materialize_blocks,
    run_b_pipeline,
    validate_beats,
    validate_visuals,
)

SCRIPT = "  Uno.\n\nDos.  "


def b11() -> B11Output:
    return B11Output.model_validate(
        {
            "pipeline_stage": "B1.1",
            "narrative_core": {
                "central_question": "¿Qué pasa?", "final_answer": "Ocurren dos cosas."
            },
            "blocks": [
                {
                    "block_id": 1,
                    "type": "intro",
                    "emotional_entry": "Sin información previa",
                    "emotional_exit": "Conoce el primer hecho",
                    "span": {"first_words": "Uno.", "last_words": "Uno."},
                },
                {
                    "block_id": 2,
                    "type": "close",
                    "emotional_entry": "Conoce el primer hecho",
                    "emotional_exit": "Conoce el desenlace",
                    "span": {"first_words": "Dos.", "last_words": "Dos."},
                },
            ],
            "error": None,
        }
    )


class FakeProvider:
    def __init__(self) -> None:
        self.b12_started = set()
        self.b12_finished = set()
        self.b2_started = set()
        self.b2_both_started = asyncio.Event()
        self.b12_both_started = asyncio.Event()
        self.calls = []

    async def generate_structured(self, *, model, instructions, input_text, output_type):
        payload = json.loads(input_text)
        self.calls.append((model, instructions, payload, output_type))
        if output_type is B11Output:
            assert payload == {"plain_script_for_recording": SCRIPT}
            return b11()
        if output_type is B12Output:
            assert set(payload) == {"narrative_core", "current_block_id", "blocks"}
            assert len(payload["blocks"]) == 1
            assert "span" not in payload["blocks"][0]
            block_id = payload["current_block_id"]
            self.b12_started.add(block_id)
            if len(self.b12_started) == 2:
                self.b12_both_started.set()
            await asyncio.wait_for(self.b12_both_started.wait(), timeout=1.0)
            self.b12_finished.add(block_id)
            return B12Output.model_validate(
                {
                    "pipeline_stage": "B1.2",
                    "block_id": block_id,
                    "beats": [
                        {
                            "beat_id": f"{block_id}A",
                            "text": payload["blocks"][0]["text"],
                            "beat_type": "orientation",
                        }
                    ],
                    "error": None,
                }
            )
        assert output_type is B2Output
        assert self.b12_finished == {1, 2}, "B2 began before all B1.2 calls completed"
        self.b2_started.add(payload["current_block_id"])
        if len(self.b2_started) == 2:
            self.b2_both_started.set()
        await asyncio.wait_for(self.b2_both_started.wait(), timeout=1.0)
        assert set(payload) == {"narrative_core", "current_block_id", "blocks"}
        assert len(payload["blocks"]) == 1
        block = payload["blocks"][0]
        if block["type"] == "intro":
            block["beats"][0].update(visual_type="avatar", description=None)
        else:
            block["beats"][0].update(visual_type="avatar", description=None)
        return B2Output.model_validate(
            {
                "pipeline_stage": "B2",
                "narrative_core": payload["narrative_core"],
                "blocks": [block],
                "error": None,
            }
        )


def test_pipeline_parallel_calls_exact_source_and_medium_model_routing() -> None:
    fake = FakeProvider()
    result = asyncio.run(run_b_pipeline(SCRIPT, provider=fake, model="gpt-6-luna"))
    assert fake.b12_started == {1, 2}
    assert fake.b2_started == {1, 2}
    assert [r.block_id for r in result.b12] == [1, 2]
    assert [r.blocks[0].block_id for r in result.b2] == [1, 2]
    assert "".join(r.beats[0].text for r in result.b12) == SCRIPT
    assert [call[0] for call in fake.calls] == ["gpt-6-luna"] * 5
    assert len(result.visual_plan()["blocks"]) == 2
    assert all("API transport:" in call[1] for call in fake.calls)


def test_exact_audited_prompt_hashes_present() -> None:
    for filename in PROMPT_CHECKSUMS:
        content = _instructions(filename)
        assert "API transport:" in content
        assert len(content) > 10_000


def test_block_materialization_preserves_leading_separator_and_trailing_spaces() -> None:
    blocks = b11().blocks
    assert blocks is not None
    materialized = materialize_blocks(SCRIPT, blocks)
    assert [block.text for block in materialized] == ["  Uno.\n\n", "Dos.  "]


def test_block_materialization_rejects_repeated_anchors() -> None:
    block = BlockMeta.model_validate(
        {
            "block_id": 1,
            "type": "development",
            "emotional_entry": "a",
            "emotional_exit": "b",
            "span": {"first_words": "Uno", "last_words": "Uno"},
        }
    )
    with pytest.raises(BPipelineValidationError, match="unique"):
        materialize_blocks("Uno Uno", [block])


def test_beat_ids_excel_sequence_and_length_rule() -> None:
    assert [_beat_suffix(n) for n in (1, 26, 27, 52, 53, 702, 703)] == [
        "A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"
    ]
    block = MaterializedBlock(
        block_id=1, type="development", emotional_entry="a", emotional_exit="b",
        text=" ".join(["palabra"] * 41),
    )
    output = B12Output(
        pipeline_stage="B1.2", block_id=1,
        beats=[Beat(beat_id="1A", text=block.text, beat_type="claim")], error=None,
    )
    with pytest.raises(BPipelineValidationError, match="length"):
        validate_beats(output, block)


def test_invalid_b2_upstream_mutation_is_rejected() -> None:
    upstream = b11()
    assert upstream.narrative_core is not None and upstream.blocks is not None
    block = upstream.blocks[0]
    supplied = B2Input.model_validate(
        {
            "narrative_core": upstream.narrative_core.model_dump(),
            "current_block_id": 1,
            "blocks": [
                {
                    **block.model_dump(),
                    "beats": [{"beat_id": "1A", "text": "Uno.", "beat_type": "claim"}],
                }
            ],
        }
    )
    tampered = supplied.blocks[0].model_dump()
    tampered["beats"][0]["text"] = "Texto inventado"
    tampered["beats"][0].update(visual_type="avatar", description=None)
    returned = B2Output.model_validate(
        {
            "pipeline_stage": "B2", "narrative_core": supplied.narrative_core.model_dump(),
            "blocks": [tampered], "error": None,
        }
    )
    with pytest.raises(BPipelineValidationError, match="frozen B1.2 beat"):
        validate_visuals(returned, supplied)


def test_invalid_intro_anchor_is_rejected() -> None:
    upstream = b11()
    assert upstream.narrative_core is not None and upstream.blocks is not None
    block = upstream.blocks[0]
    supplied = B2Input.model_validate(
        {
            "narrative_core": upstream.narrative_core.model_dump(),
            "current_block_id": 1,
            "blocks": [{**block.model_dump(), "beats": [
                {"beat_id": "1A", "text": "Uno.", "beat_type": "claim"},
            ]}],
        }
    )
    bad = supplied.blocks[0].model_dump()
    bad["beats"][0].update(visual_type="media_image", description="Una imagen realista.")
    response = B2Output.model_validate(
        {
            "pipeline_stage": "B2", "narrative_core": supplied.narrative_core.model_dump(),
            "blocks": [bad], "error": None,
        }
    )
    with pytest.raises(BPipelineValidationError, match="intro opening"):
        validate_visuals(response, supplied)


def test_strict_b12_and_b2_schemas_reject_unknown_fields() -> None:
    with pytest.raises(ValueError):
        B12Output.model_validate(
            {"pipeline_stage": "B1.2", "block_id": 1, "beats": [
                {"beat_id": "1A", "text": "Uno", "beat_type": "claim", "extra": "no"},
            ], "error": None}
        )
    with pytest.raises(ValueError):
        B2Output.model_validate(
            {
                "pipeline_stage": "B2",
                "narrative_core": {"central_question": "q", "final_answer": "a"},
             "blocks": [], "error": None}
        )


def test_crlf_script_is_reconstructed_without_newline_normalization() -> None:
    raw = " \r\nUno.\r\n\r\nDos.  "
    blocks = b11().blocks
    assert blocks is not None
    exact = materialize_blocks(raw, blocks)
    assert "".join(block.text for block in exact) == raw
    assert exact[0].text.endswith("\r\n\r\n")


def test_b12_whitespace_must_belong_to_previous_beat() -> None:
    block = MaterializedBlock(
        block_id=3, type="development", emotional_entry="a", emotional_exit="b", text="Uno. Dos."
    )
    bad = B12Output(
        pipeline_stage="B1.2", block_id=3,
        beats=[
            Beat(beat_id="3A", text="Uno.", beat_type="claim"),
            Beat(beat_id="3B", text=" Dos.", beat_type="claim"),
        ], error=None,
    )
    with pytest.raises(BPipelineValidationError, match="separator whitespace"):
        validate_beats(bad, block)
