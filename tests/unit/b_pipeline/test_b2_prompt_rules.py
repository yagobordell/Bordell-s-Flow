from types import SimpleNamespace

from ai_video_factory.bots.workflow import _b2_prompt_rule_errors


def _part(*beats):
    return SimpleNamespace(
        blocks=[SimpleNamespace(block_id=1, beats=[SimpleNamespace(**beat) for beat in beats])]
    )


def _beat(beat_id, prompt, visual_type="media_image"):
    return {"beat_id": beat_id, "visual_type": visual_type, "prompt": prompt}


def test_clean_prompt_passes():
    part = _part(_beat(
        "1A",
        "An adult in their 40s wearing a gray sweater sits at a kitchen table, "
        "holding a phone to one ear in window light.",
    ))
    assert _b2_prompt_rule_errors([part]) == []


def test_label_before_frame_is_flagged():
    part = _part(_beat("1A", "CTA continuity image 1: A kitchen table in daylight."))
    errors = _b2_prompt_rule_errors([part])
    assert len(errors) == 1 and errors[0].startswith("B2 beat 1A:")
    assert "begins with a label" in errors[0]


def test_back_reference_is_flagged_but_same_room_is_allowed():
    back = _part(_beat(
        "2A", "The same adult operator in his 30s, wearing a dark shirt, works at a desk."
    ))
    assert "refers back" in _b2_prompt_rule_errors([back])[0]

    positional = _part(_beat(
        "2B", "A man in his 30s wearing a dark shirt sits in the same room as a colleague."
    ))
    assert _b2_prompt_rule_errors([positional]) == []


def test_negation_in_visible_frame_is_flagged_but_not_in_context_note():
    visible = _part(_beat(
        "3A", "A closed envelope on a desk with no stamp visible, in daylight."
    ))
    assert "negation or absence" in _b2_prompt_rule_errors([visible])[0]

    note = _part(_beat(
        "3B",
        "A closed envelope on a desk in daylight. (For context: The sender does not "
        "buy residential accounts.)",
    ))
    assert _b2_prompt_rule_errors([note]) == []


def test_person_without_age_or_clothing_is_flagged():
    part = _part(_beat("4A", "A man sits at a desk, leaning forward toward a monitor."))
    error = _b2_prompt_rule_errors([part])[0]
    assert "without an apparent age range" in error
    assert "without clothing" in error


def test_plural_clothing_and_customer_service_are_handled():
    coats = _part(_beat(
        "5A", "A retired couple in their 60s wearing everyday coats stands at an entry."
    ))
    assert _b2_prompt_rule_errors([coats]) == []

    service = _part(_beat(
        "5B", "A bank customer service center with rows of desks under bright lights."
    ))
    assert _b2_prompt_rule_errors([service]) == []


def test_clock_hand_is_not_a_person_hand():
    part = _part(_beat(
        "6A", "A kitchen timer on a counter, its hand marking a short interval, in daylight."
    ))
    assert _b2_prompt_rule_errors([part]) == []


def test_catalog_adjectives_are_flagged():
    part = _part(_beat(
        "7A", "A photorealistic close view of a smartphone on a table in daylight."
    ))
    assert "photographic-quality" in _b2_prompt_rule_errors([part])[0]


def test_avatar_and_empty_prompts_are_ignored():
    part = _part(
        _beat("8A", "no prompt here, no person", visual_type="avatar"),
        _beat("8B", None, visual_type="media_video"),
    )
    assert _b2_prompt_rule_errors([part]) == []


from ai_video_factory.bots.workflow import (  # noqa: E402
    _B2_VISUAL_RULES_PREFIX,
    BPipelineValidationError,
    _b2_wording_failed_beat_groups,
    _b2_wording_partial_feedback,
)


def test_wording_failure_is_grouped_by_block_in_source_order():
    error = BPipelineValidationError(
        _B2_VISUAL_RULES_PREFIX
        + "B2 beat 3A: refers back to another shot ('again'); restate the subject's "
        "full description"
        + " || B2 beat 3B: uses negation or absence wording ('no') in the visible frame"
        + " || B2 beat 12C: begins with a label ('1.'); start directly with the frame description"
    )
    assert _b2_wording_failed_beat_groups(error) == {3: ("3A", "3B"), 12: ("12C",)}


def test_other_validation_errors_are_not_wording_groups():
    assert _b2_wording_failed_beat_groups(BPipelineValidationError("B2 batch changed IDs")) is None


class _Dumpable(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


def test_partial_feedback_asks_only_for_failing_beats_in_correction_schema():
    error = BPipelineValidationError(
        _B2_VISUAL_RULES_PREFIX
        + "B2 beat 3A: uses negation or absence wording ('empty') in the visible frame"
    )
    previous = SimpleNamespace(blocks=[
        SimpleNamespace(block_id=3, beats=[
            _Dumpable(beat_id="3A", text="first"),
            _Dumpable(beat_id="3C", text="third"),
        ]),
    ])
    feedback = _b2_wording_partial_feedback(error, {3: ("3A",)}, previous)
    assert "'empty'" in feedback
    assert '"beat_id":"3A"' in feedback
    assert '"beat_id":"3C"' not in feedback
    assert "Return only the listed beats" in feedback
    assert "pipeline_stage='B2'" in feedback


from ai_video_factory.bots.workflow import _b2_repetition_errors  # noqa: E402


def _frame(beat_id, prompt):
    return {"beat_id": beat_id, "visual_type": "media_image", "prompt": prompt}


def _frames(*beats):
    return SimpleNamespace(
        blocks=[SimpleNamespace(block_id=1, beats=[SimpleNamespace(**beat) for beat in beats])]
    )


def test_repeated_frame_is_flagged_on_the_later_beat():
    errors = _b2_repetition_errors([_frames(
        _frame("1A", "A smartphone rests on a kitchen table beside a debit card in window light."),
        _frame("1B", "A smartphone rests on a kitchen table beside a debit card "
               "in window light."),
    )])
    assert len(errors) == 1
    assert errors[0].startswith("B2 beat 1B:")
    assert "beat 1A" in errors[0]


def test_distinct_frames_are_not_flagged():
    errors = _b2_repetition_errors([_frames(
        _frame("1A", "A telecom technician in their 30s wears a plain shirt and traces a cable "
                     "through a switching rack."),
        _frame("1B", "A retired couple in their 60s stands at the entry of a brick house "
               "in daylight."),
    )])
    assert errors == []


def test_recurring_person_descriptor_alone_does_not_count():
    errors = _b2_repetition_errors([_frames(
        _frame("2A", "A man in his 30s wearing a charcoal shirt types at a laptop in an office corridor."),
        _frame("2B", "A man in his 30s wearing a charcoal shirt slides an envelope "
               "across a post office counter."),
    )])
    assert errors == []


def test_only_the_six_previous_beats_are_compared():
    phone = "A smartphone rests on a kitchen table beside a debit card in window light."
    scenes = [
        "A bicycle leans against a brick wall in morning light.",
        "Rain streaks across a bus window on a city street.",
        "A violin rests on a wooden chair in a concert hall.",
        "Steam rises from a teapot on a marble countertop.",
        "A map unfolds across a wide mountain trail.",
        "Lanterns hang above a fruit stall at dusk.",
    ]
    filler = [_frame(f"3{chr(66 + i)}", scene) for i, scene in enumerate(scenes)]
    errors = _b2_repetition_errors([_frames(_frame("3A", phone), *filler, _frame("3H", phone))])
    assert errors == []
