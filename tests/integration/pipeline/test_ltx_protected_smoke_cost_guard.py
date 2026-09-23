from pathlib import Path

WRAPPER = Path("scripts/smoke/run_ltx25_protected_smoke.ps1")


def test_ltx_protected_smoke_caps_model_bootstrap_before_reallocation() -> None:
    script = WRAPPER.read_text(encoding="utf-8")

    assert '$ModelBootstrapTimeoutMinutes = 45' in script
    assert '$RunningNotReadyReallocationThresholdMinutes = 60' in script
    assert 'TimeoutMinutes = $ModelBootstrapTimeoutMinutes' in script
    assert (
        'RunningNotReadyTimeoutMinutes = '
        '$RunningNotReadyReallocationThresholdMinutes'
    ) in script
    assert "Request-InstanceReallocation" not in script


def test_ltx_protected_smoke_always_stops_in_finally() -> None:
    script = WRAPPER.read_text(encoding="utf-8")

    assert '$Service = "ltx25"' in script
    assert 'Action = "Smoke"' in script
    assert 'Action = "Stop"' in script
    assert "finally {" in script
    finally_block = script.split("finally {", maxsplit=1)[1]
    assert "& $Manager @StopArguments" in finally_block
