from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_video_factory.workers.ltx25.a2v import (
    LTX_A2V_DEV_GENERATION_PROFILE,
    LTX_A2V_REFERENCE_GENERATION_PROFILE,
    LTXAudioToVideoParameters,
    select_reference_stage_2_sigmas,
)
from scripts.smoke.submit_ltx25_a2v_smoke import parse_args

ORIGINAL = (0.909375, 0.725, 0.421875, 0.0)
REDUCED = (0.909375, 0.421875, 0.0)


def test_two_step_sigma_subset_preserves_noise_endpoints_and_default() -> None:
    assert select_reference_stage_2_sigmas(ORIGINAL, 3) is ORIGINAL
    assert select_reference_stage_2_sigmas(ORIGINAL, 2) == REDUCED
    assert select_reference_stage_2_sigmas(list(ORIGINAL), 2) == list(REDUCED)


@pytest.mark.parametrize(
    "sigmas",
    [
        (1.0, 0.725, 0.421875, 0.0),
        (0.909375, 0.725, 0.421875),
        (0.909375, 0.725, 0.421875, 0.1),
    ],
)
def test_two_step_experiment_fails_closed_if_upstream_schedule_changed(
    sigmas: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError, match="schedule changed"):
        select_reference_stage_2_sigmas(sigmas, 2)


def test_two_step_experiment_only_available_with_reference_profile() -> None:
    ordinary = LTXAudioToVideoParameters(
        generation_profile=LTX_A2V_REFERENCE_GENERATION_PROFILE
    )
    assert ordinary.reference_stage_2_steps == 3

    experimental = LTXAudioToVideoParameters(
        generation_profile=LTX_A2V_REFERENCE_GENERATION_PROFILE,
        reference_stage_2_steps=2,
    )
    assert experimental.reference_stage_2_steps == 2

    with pytest.raises(ValidationError, match="requires the reference"):
        LTXAudioToVideoParameters(
            generation_profile=LTX_A2V_DEV_GENERATION_PROFILE,
            reference_stage_2_steps=2,
        )
    with pytest.raises(ValidationError, match="greater than or equal"):
        LTXAudioToVideoParameters(
            generation_profile=LTX_A2V_REFERENCE_GENERATION_PROFILE,
            reference_stage_2_steps=1,
        )
    with pytest.raises(ValidationError, match="less than or equal"):
        LTXAudioToVideoParameters(
            generation_profile=LTX_A2V_REFERENCE_GENERATION_PROFILE,
            reference_stage_2_steps=4,
        )


def test_submit_cli_defaults_unchanged_and_two_step_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["submit_ltx25_a2v_smoke.py", "--audio", "speech.wav"])
    args = parse_args()
    assert args.profile == "reference"
    assert args.reference_stage2_steps == 3

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_ltx25_a2v_smoke.py",
            "--audio",
            "speech.wav",
            "--profile",
            "reference",
            "--reference-stage2-steps",
            "2",
        ],
    )
    assert parse_args().reference_stage2_steps == 2


def test_controlled_wrapper_only_forwards_experimental_steps_on_opt_in() -> None:
    wrapper = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    submit = Path("scripts/smoke/submit_ltx25_a2v_smoke.py").read_text(
        encoding="utf-8"
    )
    assert "[ValidateSet(2, 3)][int]$ReferenceStage2Steps = 3" in wrapper
    assert "Experimental Stage 2 requires an immutable -ExpectedPinnedImage" in wrapper
    assert '$Arguments += @("--reference-stage2-steps", "2")' in wrapper
    assert 'request_parameters["reference_stage_2_steps"] = 2' in submit
    assert "experimental Stage-2 sigma subset differs from reviewed recipe" in submit
    assert 'metadata.get("reference_stage_2_steps_experimental") is not True' in submit
