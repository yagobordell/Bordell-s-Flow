import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_video_factory.bots.billing import ApiCostLedger
from ai_video_factory.bots.contracts import (
    B2Input,
    B2Output,
    B11Output,
    B12Output,
    Beat,
    BlockMeta,
    MaterializedBlock,
    WholeB12Output,
)
from ai_video_factory.bots.workflow import (
    BPipelineValidationError,
    _beat_suffix,
    _instructions,
    materialize_blocks,
    repair_frozen_b2_text,
    run_b_pipeline,
    validate_beats,
    validate_visuals,
)
from ai_video_factory.config import settings

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

    async def generate_structured(
        self, *, model, instructions, input_text, output_type, input_image_url=None
    ):
        payload = json.loads(input_text)
        self.calls.append((model, instructions, payload, output_type, input_image_url))
        if output_type is B11Output:
            assert payload == {"plain_script_for_recording": SCRIPT}
            return b11()
        if output_type is WholeB12Output:
            assert set(payload) == {"plain_script_for_recording", "narrative_core", "blocks"}
            assert payload["plain_script_for_recording"] == SCRIPT
            assert len(payload["blocks"]) == 2
            self.b12_started.update({1, 2})
            self.b12_both_started.set()
            self.b12_finished.update({1, 2})
            return WholeB12Output.model_validate({
                "pipeline_stage": "B1.2",
                "narrative_core": payload["narrative_core"],
                "blocks": [
                    {"block_id": block["block_id"], "beats": [{
                        "beat_id": f"{block['block_id']}A",
                        "text": block["text"],
                        "beat_type": "orientation",
                    }]}
                    for block in payload["blocks"]
                ],
                "error": None,
            })
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


def test_pipeline_parallel_calls_exact_source_and_medium_model_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_model", "gpt-6-luna")
    fake = FakeProvider()
    result = asyncio.run(run_b_pipeline(SCRIPT, provider=fake))
    assert fake.b12_started == {1, 2}
    assert fake.b2_started == {1, 2}
    assert [r.block_id for r in result.b12] == [1, 2]
    assert [r.blocks[0].block_id for r in result.b2] == [1, 2]
    assert "".join(r.beats[0].text for r in result.b12) == SCRIPT
    assert [call[0] for call in fake.calls] == ["gpt-6-luna"] * 4
    assert len(result.visual_plan()["blocks"]) == 2
    assert all("API transport:" in call[1] for call in fake.calls)
    merged_beats = result.merged_b12_output()
    merged_visuals = result.merged_b2_output()
    assert merged_beats["pipeline_stage"] == "B1.2"
    assert [block["block_id"] for block in merged_beats["blocks"]] == [1, 2]
    assert [block["beats"][0]["beat_id"] for block in merged_beats["blocks"]] == [
        "1A",
        "2A",
    ]
    assert merged_visuals["pipeline_stage"] == "B2"
    assert [block["block_id"] for block in merged_visuals["blocks"]] == [1, 2]
    assert [block["beats"][0]["visual_type"] for block in merged_visuals["blocks"]] == [
        "avatar",
        "avatar",
    ]
    assert merged_beats["narrative_core"] == merged_visuals["narrative_core"]


def test_casero_b2_attaches_selected_avatar_reference() -> None:
    fake = FakeProvider()

    asyncio.run(
        run_b_pipeline(
            SCRIPT,
            provider=fake,
            visual_style="casero",
            avatar_image=b"avatar-png-bytes",
        )
    )

    b2_calls = [call for call in fake.calls if call[3] is B2Output]
    assert len(b2_calls) == 2
    assert all(
        call[4] == "data:image/png;base64,YXZhdGFyLXBuZy1ieXRlcw=="
        for call in b2_calls
    )


def test_b2_schema_validation_error_is_retried_once() -> None:
    class SchemaRetryProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.injected_schema_error = False

        async def generate_structured(
            self, *, model, instructions, input_text, output_type, input_image_url=None
        ):
            payload = json.loads(input_text)
            if (
                output_type is B2Output
                and payload["current_block_id"] == 1
                and not self.injected_schema_error
            ):
                self.injected_schema_error = True
                block = payload["blocks"][0]
                block["beats"][0].update(
                    visual_type="media_image",
                    description=None,
                )
                return B2Output.model_validate(
                    {
                        "pipeline_stage": "B2",
                        "narrative_core": payload["narrative_core"],
                        "blocks": [block],
                        "error": None,
                    }
                )
            return await super().generate_structured(
                model=model,
                instructions=instructions,
                input_text=input_text,
                output_type=output_type,
                input_image_url=input_image_url,
            )

    rejected: list[tuple[int, int, str, dict[str, object]]] = []
    provider = SchemaRetryProvider()
    result = asyncio.run(
        run_b_pipeline(
            SCRIPT,
            provider=provider,
            avatar_image=b"avatar-png-bytes",
            on_rejected_b2_output=lambda block_id, attempt, error, payload: rejected.append(
                (block_id, attempt, error, payload)
            ),
        )
    )

    assert provider.injected_schema_error
    assert len(rejected) == 1
    block_id, attempt, error, payload = rejected[0]
    assert block_id == 1
    assert attempt == 1
    assert "non-avatar visual requires a nonempty description" in error
    assert payload["unparsed_response"] is True
    assert payload["schema_errors"]
    assert [item.blocks[0].block_id for item in result.b2] == [1, 2]


@pytest.mark.parametrize(
    ("start", "stop", "expected_stages"),
    [
        ("B1.1", "B1.1", ["B1.1"]),
        ("B1.1", "B1.2", ["B1.1", "B1.2"]),
        ("B1.2", "B1.2", ["B1.2"]),
        ("B2", "B2", ["B2"]),
    ],
)
def test_stop_stage_never_starts_the_next_planning_wave(
    start: str, stop: str, expected_stages: list[str]
) -> None:
    previous_b12 = None
    if start == "B2":
        checkpoint = asyncio.run(
            run_b_pipeline(SCRIPT, provider=FakeProvider(), stop_after="B1.2")
        )
        previous_b12 = checkpoint.b12
    provider = FakeProvider()
    if start == "B2":
        provider.b12_finished = {1, 2}
    stages: list[str] = []
    result = asyncio.run(
        run_b_pipeline(
            SCRIPT,
            provider=provider,
            start_from=start,
            stop_after=stop,
            previous_b11=b11() if start != "B1.1" else None,
            previous_b12=previous_b12,
            on_stage_complete=lambda stage, _blocks, _merged: stages.append(stage),
        )
    )
    assert stages == expected_stages
    assert len(result.b12) == (0 if stop == "B1.1" else 2)
    assert len(result.b2) == (2 if stop == "B2" else 0)
    assert len(provider.calls) == (
        (1 if start == "B1.1" else 0)
        + (1 if start in ("B1.1", "B1.2") and stop != "B1.1" else 0)
        + (2 if stop == "B2" else 0)
    )


def test_stop_stage_rejects_a_range_that_goes_backwards() -> None:
    with pytest.raises(ValueError, match="--stop cannot be earlier"):
        asyncio.run(
            run_b_pipeline(
                SCRIPT, provider=FakeProvider(), start_from="B2", stop_after="B1.2"
            )
        )


def test_current_prompt_files_load_with_transport_instructions() -> None:
    for filename in ("b1_1.md", "b1_2.md", "b2.md"):
        content = _instructions(filename)
        assert "API transport:" in content
        assert len(content) > 10_000


def test_block_materialization_preserves_leading_separator_and_trailing_spaces() -> None:
    blocks = b11().blocks
    assert blocks is not None
    materialized = materialize_blocks(SCRIPT, blocks)
    assert [block.text for block in materialized] == ["  Uno.\n\n", "Dos.  "]


def test_block_materialization_resolves_repeated_and_wrapped_anchors() -> None:
    blocks = [
        BlockMeta.model_validate(
            {
                "block_id": 1,
                "type": "intro",
                "emotional_entry": "a",
                "emotional_exit": "b",
                "span": {
                    "first_words": "Inicio. Repetido.",
                    "last_words": "Fin uno.",
                },
            }
        ),
        BlockMeta.model_validate(
            {
                "block_id": 2,
                "type": "close",
                "emotional_entry": "b",
                "emotional_exit": "c",
                "span": {"first_words": "Repetido.", "last_words": "Fin dos."},
            }
        ),
    ]
    raw = "Inicio.\r\nRepetido. Fin uno. Repetido. Fin dos."
    materialized = materialize_blocks(raw, blocks)
    assert [block.text for block in materialized] == [
        "Inicio.\r\nRepetido. Fin uno. ",
        "Repetido. Fin dos.",
    ]


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
    with pytest.raises(BPipelineValidationError, match="maximum 40"):
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
    repaired = repair_frozen_b2_text(returned, supplied)
    validate_visuals(repaired, supplied)
    assert repaired.blocks[0].beats[0].text == "Uno."


def test_intro_first_beat_is_not_forced_to_avatar() -> None:
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
    visual = supplied.blocks[0].model_dump()
    visual["beats"][0].update(
        visual_type="media_image", description="Una imagen documental realista."
    )
    response = B2Output.model_validate(
        {
            "pipeline_stage": "B2", "narrative_core": supplied.narrative_core.model_dump(),
            "blocks": [visual], "error": None,
        }
    )
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


def test_b12_ignores_whitespace_and_punctuation_differences() -> None:
    block = MaterializedBlock(
        block_id=3, type="development", emotional_entry="a", emotional_exit="b",
        text="La primera frase es completa. La segunda frase también es completa."
    )
    bad = B12Output(
        pipeline_stage="B1.2", block_id=3,
        beats=[
            Beat(beat_id="3A", text="La primera frase es completa, ", beat_type="claim"),
            Beat(beat_id="3B", text="\nLa segunda frase también es completa", beat_type="claim"),
        ], error=None,
    )
    assert validate_beats(bad, block) == bad.beats


def test_metered_parallel_pipeline_records_every_exact_input_output() -> None:
    class MeteredFake(FakeProvider):
        async def generate_structured_with_response(self, **kwargs):
            output = await self.generate_structured(**kwargs)
            response = SimpleNamespace(
                id=f"resp_{len(self.calls)}",
                model="gpt-6-luna",
                service_tier="default",
                usage=SimpleNamespace(
                    input_tokens=100,
                    input_tokens_details=SimpleNamespace(
                        cached_tokens=0, cache_write_tokens=0
                    ),
                    output_tokens=20,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=5),
                ),
            )
            return output, response

    fake = MeteredFake()
    ledger = ApiCostLedger()
    inputs = {}
    outputs = {}
    stage_times = {}
    call_times = {}
    stage_events: list[str] = []

    def stage_complete(stage: str, blocks: int, merged: dict | None) -> None:
        stage_events.append(stage)
        assert blocks == 2
        if stage == "B1.1":
            assert merged is None
            assert not fake.b12_started
        elif stage == "B1.2":
            assert fake.b12_finished == {1, 2}
            assert not fake.b2_started
            assert merged is not None
            assert [block["block_id"] for block in merged["blocks"]] == [1, 2]
        else:
            assert stage == "B2"
            assert fake.b2_started == {1, 2}
            assert merged is not None
            assert [block["block_id"] for block in merged["blocks"]] == [1, 2]

    result = asyncio.run(
        run_b_pipeline(
            SCRIPT,
            provider=fake,
            on_input=lambda stage, block_id, payload: inputs.update(
                {(stage, block_id): payload}
            ),
            on_output=lambda stage, block_id, payload: outputs.update(
                {(stage, block_id): payload}
            ),
            on_api_response=ledger.record,
            on_stage_duration=lambda stage, seconds: stage_times.update({stage: seconds}),
            on_stage_complete=stage_complete,
            on_call_duration=lambda stage, block_id, seconds: call_times.update(
                {(stage, block_id): seconds}
            ),
        )
    )

    expected_calls = {("B1.1", None), ("B1.2", None), ("B2", 1), ("B2", 2)}
    expected_outputs = expected_calls | {("B1.2", 1), ("B1.2", 2)}
    assert set(inputs) == expected_calls
    assert set(outputs) == expected_outputs
    assert inputs["B1.1", None] == {"plain_script_for_recording": SCRIPT}
    assert inputs["B1.2", None]["blocks"][0]["text"] == "  Uno.\n\n"
    assert inputs["B1.2", None]["blocks"][1]["text"] == "Dos.  "
    assert outputs["B1.1", None] == result.b11.model_dump()
    assert outputs["B1.2", 1] == result.b12[0].model_dump()
    assert outputs["B2", 2] == result.b2[1].model_dump()
    assert inputs["B2", 1]["blocks"][0]["beats"][0]["beat_id"] == "1A"
    assert len(ledger.records) == 4
    assert stage_events == ["B1.1", "B1.2", "B2"]
    assert set(stage_times) == {"B1.1", "B1.2", "B2"}
    assert set(call_times) == expected_calls
    assert all(seconds >= 0 for seconds in stage_times.values())
    assert all(seconds >= 0 for seconds in call_times.values())
    costs = ledger.report(
        blocks=2, run_status="completed",
        expected_calls={"B1.1": 1, "B1.2": 1, "B2": 2},
    )
    assert costs["estimated_total_usd"] == "0.00008000"
    assert costs["stages"]["B1.1"]["recorded_calls"] == 1
    assert costs["stages"]["B1.2"]["recorded_calls"] == 1
    assert costs["stages"]["B2"]["recorded_calls"] == 2
