import subprocess
import sys
from pathlib import Path

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

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_ltx25_a2v_smoke.py",
            "--audio",
            "speech.wav",
            "--profile",
            "reference-compiled",
        ],
    )
    assert parse_args().profile == "reference-compiled"

    monkeypatch.setattr(
        sys,
        "argv",
        ["submit_ltx25_a2v_smoke.py", "--audio", "speech.wav", "--profile", "guided"],
    )
    guided_args = parse_args()
    assert guided_args.profile == "guided"
    assert guided_args.pending_timeout_seconds is None

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "submit_ltx25_a2v_smoke.py",
            "--audio",
            "speech.wav",
            "--profile",
            "guided",
            "--pending-timeout-seconds",
            "7200",
        ],
    )
    assert parse_args().pending_timeout_seconds == 7200.0


def test_controlled_a2v_smoke_checks_local_python_before_gpu_allocation() -> None:
    script = Path("scripts/smoke/run_ltx25_a2v_smoke_controlled.ps1").read_text(
        encoding="utf-8"
    )
    source_check = Path("scripts/smoke/check_ltx25_python_source.py").read_text(
        encoding="utf-8"
    )
    assert "LTX_A2V_GUIDED_GENERATION_PROFILE" in source_check
    assert "check_ltx25_python_source.py" in script
    assert "reference-compiled" in script
    assert "Refusing GPU allocation" in script
    assert "ExpectedPinnedImage" in script
    assert "AllowBootstrappingInstance = $true" in script
    assert script.index("$ReadyWait = ") < script.index("& $WorkerManager @Start")
    assert script.index('if ($Profile -eq "reference-compiled") {') < script.index(
        "& $WorkerManager @Start"
    )
    assert "python -c" not in script
    assert script.index("& $Python $SourceCheck $ExpectedLtxModule") < script.index(
        "& $Python $R2Preflight"
    )
    assert script.index("& $Python $SourceCheck $ExpectedLtxModule") < script.index(
        "& $WorkerManager @Start"
    )


def test_ltx_smoke_python_source_check_accepts_matching_worktree(tmp_path: Path) -> None:
    import ai_video_factory.workers.ltx25 as ltx25

    script = Path("scripts/smoke/check_ltx25_python_source.py")
    expected = Path(ltx25.__file__).resolve()
    accepted = subprocess.run(
        [sys.executable, str(script), str(expected)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr

    rejected = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "other" / "__init__.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "Wrong LTX Python source" in rejected.stderr
