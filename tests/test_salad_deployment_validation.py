from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from ai_video_factory.providers.ideogram_caption import validate_ideogram_caption

SMOKE_SCRIPT = Path("scripts/run_salad_smoke_suite.py")
VALIDATION_MANAGER = Path("scripts/manage_salad_validation.ps1")
SCALE_TO_ZERO_STARTER = Path("scripts/start_salad_scale_to_zero.ps1")
PROTECTED_BOOTSTRAP = Path("scripts/start_salad_protected_smoke.ps1")
SCALE_TO_ZERO_RESTORER = Path("scripts/restore_salad_scale_to_zero.ps1")


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
    action_set = (
        'ValidateSet("Validate", "Prepare", "Start", "Prewarm", "Status", "Smoke", '
        '"ProtectedSmoke", "Stop")'
    )

    assert action_set in text
    service_set = (
        'ValidateSet("whisper", "breeze_tts2", "fish_speech", "ideogram4", '
        '"flux2_klein", "ltx25", "realesrgan", "all")'
    )
    assert service_set in text
    assert 'python scripts/run_salad_smoke_suite.py' in text
    assert "manage_salad_stack.ps1" in text
    assert "start_salad_scale_to_zero.ps1" in text
    assert "start_salad_protected_smoke.ps1" in text
    assert "restore_salad_scale_to_zero.ps1" in text
    assert "ensure_salad_zero_replicas.ps1" in text
    assert '$CallSucceeded = $?' in text
    assert 'if (-not $CallSucceeded)' in text
    assert '"Prepare" { Invoke-StackAction -StackAction "Prepare" }' in text
    assert '"Start" { Invoke-ScaleToZeroStart }' in text
    assert '"Prewarm" { Invoke-ProtectedSmokeBootstrap }' in text
    assert '"ProtectedSmoke" { Invoke-ProtectedSmoke }' in text
    assert '"Stop" { Invoke-SafeStop }' in text
    assert 'ValidateSet("Full"' not in text


def test_scale_to_zero_start_keeps_idle_deploying_for_normal_start() -> None:
    text = SCALE_TO_ZERO_STARTER.read_text(encoding="utf-8")

    assert "function Test-ScaleToZeroActive" in text
    assert '[int]$Definition.autoscaler.min_replicas -ne 0' in text
    assert '$Status -ne "deploying"' in text
    assert '$Status -eq "running"' in text
    assert 'return [int]$Group.replicas -eq 0' in text
    assert '"$GroupUrl/start"' in text
    assert "first queued job may trigger a cold start" in text


def test_protected_smoke_bootstraps_through_manual_replica_and_real_instance() -> None:
    bootstrap = PROTECTED_BOOTSTRAP.read_text(encoding="utf-8")
    manager = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "@{ replicas = 1 }" in bootstrap
    assert "min_replicas = 1" not in bootstrap
    assert '$Group.PSObject.Properties["queue_autoscaler"]' in bootstrap
    assert "Test-RemoteAutoscalerMinReplicas" in bootstrap
    assert "-ExpectedMinReplicas 0" in bootstrap
    assert "$Group.queue_autoscaler" not in bootstrap
    assert '"$GroupUrl/start"' in bootstrap
    assert '"$GroupUrl/instances"' in bootstrap
    assert "Test-QueueAttachment" in bootstrap
    assert "current_queue_length" in bootstrap
    assert "$StartedInstances.Count -eq 1" in bootstrap
    assert "$Instances.Count -gt 1" in bootstrap

    protected = manager.split("function Invoke-ProtectedSmoke {", maxsplit=1)[1].split(
        "function Invoke-SafeStop", maxsplit=1
    )[0]
    assert '$Service -eq "all"' in protected
    assert "Invoke-ProtectedSmokeBootstrap" in protected
    assert "Invoke-Smoke" in protected
    assert "Invoke-ScaleToZeroRestore" in protected
    assert "finally" in protected
    assert 'Invoke-StackAction -StackAction "Stop"' in protected
    assert "Invoke-ZeroReplicaFallback -StopFailure $_" in protected
    assert protected.index("Invoke-ProtectedSmokeBootstrap") < protected.index("Invoke-Smoke")
    assert protected.index("Invoke-Smoke") < protected.index("Invoke-ScaleToZeroRestore")
    assert protected.index("Invoke-ScaleToZeroRestore") < protected.index(
        'Invoke-StackAction -StackAction "Stop"'
    )


def test_scale_to_zero_restore_reinstates_manifest_autoscaler() -> None:
    restorer = SCALE_TO_ZERO_RESTORER.read_text(encoding="utf-8")
    manager = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "function New-ManifestAutoscaler" in restorer
    assert "function Test-ManifestAutoscaler" in restorer
    assert "Definition.autoscaler.min_replicas" in restorer
    assert "queue_autoscaler = New-ManifestAutoscaler" in restorer
    assert '-Method "Patch"' in restorer
    assert 'Operation "restore manifest autoscaler"' in restorer

    safe_stop = manager.split("function Invoke-SafeStop", maxsplit=1)[1].split(
        "switch ($Action)", maxsplit=1
    )[0]
    assert "Invoke-ScaleToZeroRestore" in safe_stop
    assert 'Invoke-StackAction -StackAction "Stop"' in safe_stop
    assert "Invoke-ZeroReplicaFallback -StopFailure $_" in safe_stop
    assert safe_stop.index("Invoke-ScaleToZeroRestore") < safe_stop.index(
        'Invoke-StackAction -StackAction "Stop"'
    )


def test_smoke_suite_persists_evidence_for_each_worker() -> None:
    text = SMOKE_SCRIPT.read_text(encoding="utf-8")

    for service in ("breeze_tts2", "whisper", "ideogram4", "ltx25"):
        assert f'_write_report(args.output_dir, "{service}"' in text
    assert '"smoke-summary.json"' in text


def test_validation_manager_exposes_targeted_prepare_recreate() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "[switch]$Recreate" in text
    assert '$Arguments["Recreate"] = $true' in text
    assert "-Recreate is only valid with -Action Prepare." in text
    assert "-Recreate requires one explicit service" in text



def test_validation_manager_forwards_explicit_pinned_image_for_recovery() -> None:
    text = VALIDATION_MANAGER.read_text(encoding="utf-8")

    assert "[string]$PinnedImage" in text
    assert '$Arguments["PinnedImage"] = $PinnedImage' in text
    assert "-PinnedImage requires one explicit service." in text
