from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from ai_video_factory.providers.ideogram_caption import validate_ideogram_caption

SMOKE_SCRIPT = Path("scripts/run_salad_smoke_suite.py")
VALIDATION_MANAGER = Path("scripts/manage_salad_validation.ps1")


def _load_smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_salad_smoke_suite", SMOKE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_suite_uses_dependency_order_and_real_workers() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert '_SERVICE_ORDER = ("breeze_tts2", "whisper", "ideogram4", "ltx25")' in text
    assert "SaladBreezeSpeechProvider" in text
    assert "SaladWhisperTranscriptionProvider" in text
    assert "SaladIdeogramImageProvider" in text
    assert '"scripts/submit_ltx25_smoke.py"' in text
    assert '"1024x1536"' in text
    assert 'quality="high"' in text


def test_smoke_ideogram_caption_matches_local_contract() -> None:
    module = _load_smoke_module()

    caption = module._smoke_caption()

    assert validate_ideogram_caption(caption) == caption


def test_validation_manager_keeps_expensive_actions_explicit() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")
    action_set = 'ValidateSet("Validate", "Prepare", "Start", "Status", "Smoke", "Stop")'

    assert action_set in text
    assert 'ValidateSet("whisper", "breeze_tts2", "ideogram4", "ltx25", "all")' in text
    assert 'python scripts/run_salad_smoke_suite.py' in text
    assert "manage_salad_stack.ps1" in text
    assert '"Prepare" { Invoke-StackAction -StackAction "Prepare" }' in text
    assert '"Start" { Invoke-StackAction -StackAction "Start" }' in text
    assert '"Stop" { Invoke-StackAction -StackAction "Stop" }' in text
    assert 'ValidateSet("Full"' not in text


def test_smoke_suite_persists_evidence_for_each_worker() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    for service in ("breeze_tts2", "whisper", "ideogram4", "ltx25"):
        assert f'_write_report(args.output_dir, "{service}"' in text
    assert '"smoke-summary.json"' in text
