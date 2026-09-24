import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
CONFIG = Path("src/ai_video_factory/config.py")
QWEN_CONTROLLER = Path("scripts/pipeline/_qwen_controlled.ps1")
QWEN_WRAPPERS = (
    Path("scripts/pipeline/run_phase4_assets_controlled.ps1"),
    Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1"),
)


def test_phase6_qwen_inference_has_bounded_pending_and_running_timeouts() -> None:
    text = Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1").read_text(
        encoding="utf-8"
    )
    runner = Path("scripts/pipeline/run_phase6_keyframes.py").read_text(encoding="utf-8")

    assert "[int]$PendingTimeoutSeconds = 1800" in text
    assert "[int]$RunningTimeoutSeconds = 3600" in text
    assert '"--pending-timeout-seconds", $PendingTimeoutSeconds' in text
    assert '"--timeout-seconds", $RunningTimeoutSeconds' in text
    assert "DEFAULT_QWEN_PENDING_TIMEOUT_SECONDS = 1800.0" in runner
    assert "SaladQwenImage21Provider" in runner


def test_phase6_emits_inference_progress_logs() -> None:
    runner = Path("scripts/pipeline/run_phase6_keyframes.py").read_text(encoding="utf-8")
    executor = Path(
        "src/ai_video_factory/providers/inference_jobs.py"
    ).read_text(encoding="utf-8")

    assert "logging.basicConfig(" in runner
    assert "Inference transport submitted" in executor
    assert "Inference transport progress" in executor
    assert "elapsed_seconds=%.1f" in executor


def test_phase5_alignment_pins_canonical_salad_route() -> None:
    text = Path("scripts/pipeline/run_phase5_alignment_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert "deploy\\salad\\services.json" in text
    assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
    assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
    assert "$env:SALAD_WHISPER_QUEUE_NAME = [string]$WhisperService.queue_name" in text
    assert '"--queue-name", $env:SALAD_WHISPER_QUEUE_NAME' in text
    assert "Phase 5 canonical Salad route" in text


def test_whisper_default_queue_matches_manifest() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]
    config = CONFIG.read_text(encoding="utf-8")

    expected = services["whisper"]["queue_name"]
    assert f'salad_whisper_queue_name: str = "{expected}"' in config


def test_qwen_controlled_runners_pin_canonical_salad_route() -> None:
    text = QWEN_CONTROLLER.read_text(encoding="utf-8")
    assert "deploy\\salad\\services.json" in text
    assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
    assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
    assert "$env:SALAD_QWEN_IMAGE_21_QUEUE_NAME = [string]$QwenService.queue_name" in text
    assert '"--queue-name", $env:SALAD_QWEN_IMAGE_21_QUEUE_NAME' in text
    assert "canonical Salad route: qwen_image_21 queue={1}" in text
    for path, phase in zip(QWEN_WRAPPERS, ("Phase 4", "Phase 6"), strict=True):
        wrapper = path.read_text(encoding="utf-8")
        assert f'Phase = "{phase}"' in wrapper
        assert "_qwen_controlled.ps1" in wrapper


def test_image_queue_defaults_match_manifest() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]
    config = CONFIG.read_text(encoding="utf-8")

    qwen = services["qwen_image_21"]["queue_name"]
    ideogram = services["ideogram4"]["queue_name"]
    assert f'salad_qwen_image_21_queue_name: str = "{qwen}"' in config
    assert f'salad_ideogram4_queue_name: str = "{ideogram}"' in config
