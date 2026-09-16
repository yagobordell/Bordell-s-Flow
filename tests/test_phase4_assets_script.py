from pathlib import Path

PHASE4_SCRIPT = Path("scripts/run_phase4_assets.py")
PHASE6_SCRIPT = Path("scripts/run_phase6_keyframes.py")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = PHASE4_SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text


def test_ideogram_clients_default_to_ninety_minute_pending_budget() -> None:
    for script in (PHASE4_SCRIPT, PHASE6_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS = 5400.0" in text
        assert "default=DEFAULT_IDEOGRAM_PENDING_TIMEOUT_SECONDS" in text
