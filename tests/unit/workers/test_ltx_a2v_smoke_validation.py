import sys

import pytest

from scripts.smoke.submit_ltx25_a2v_smoke import _validate_voice_tail_profiles, parse_args


def test_reference_voice_tail_allows_aac_variation_but_rejects_known_truncation() -> None:
    # Five 20 ms active windows end the speech. The AAC-like profile preserves
    # their timing with lower energy, while the truncated profile drops the final
    # 40 ms without changing the earlier envelope.
    input_rms = [0.0] * 10 + [1200.0, 1050.0, 900.0, 750.0, 600.0] + [0.0] * 8
    aac_like = [0.0] * 10 + [980.0, 860.0, 760.0, 620.0, 500.0] + [0.0] * 8

    _validate_voice_tail_profiles(
        input_rms,
        aac_like,
        window_seconds=0.02,
    )

    truncated = [0.0] * 10 + [980.0, 860.0, 760.0, 0.0, 0.0] + [0.0] * 8
    with pytest.raises(RuntimeError, match="final voiced tail"):
        _validate_voice_tail_profiles(
            input_rms,
            truncated,
            window_seconds=0.02,
        )


def test_a2v_smoke_defaults_to_reference_and_fast_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["submit_ltx25_a2v_smoke.py", "--audio", "speech.wav"])
    assert parse_args().profile == "reference"

    monkeypatch.setattr(
        sys,
        "argv",
        ["submit_ltx25_a2v_smoke.py", "--audio", "speech.wav", "--profile", "fast"],
    )
    assert parse_args().profile == "fast"
