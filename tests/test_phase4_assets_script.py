from pathlib import Path


SCRIPT = Path("scripts/run_phase4_assets.py")


def test_phase4_assets_accepts_utf8_bom() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8-sig")' in text
