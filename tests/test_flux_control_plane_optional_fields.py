from pathlib import Path

PREWARM = Path("scripts/start_salad_flux2_klein_prewarm.ps1")
RESTORE = Path("scripts/restore_salad_flux2_klein_scale_to_zero.ps1")
PHASE4 = Path("scripts/run_phase4_assets_controlled.ps1")
PHASE6 = Path("scripts/run_phase6_keyframes_controlled.ps1")


def test_flux_prewarm_uses_group_queue_connection_not_queue_container_groups() -> None:
    script = PREWARM.read_text(encoding="utf-8")

    assert '$Group.PSObject.Properties["queue_connection"]' in script
    assert "Queue.container_groups" not in script
    assert "$Queue.container_groups" not in script
    assert "Get-QueueConnectionName -Group $Group" in script


def test_flux_prewarm_tolerates_hidden_queue_autoscaler_after_patch() -> None:
    script = PREWARM.read_text(encoding="utf-8")

    assert '$Group.PSObject.Properties["queue_autoscaler"]' in script
    assert "Salad GET does not expose queue_autoscaler" in script
    assert "$Group.queue_autoscaler.min_replicas" not in script


def test_flux_restore_is_idempotent_when_queue_autoscaler_is_omitted() -> None:
    script = RESTORE.read_text(encoding="utf-8")

    assert "FLUX restore: applying manifest queue autoscaler settings" in script
    assert '$Group.PSObject.Properties["queue_autoscaler"]' in script
    assert "Salad GET does not expose queue_autoscaler" in script
    assert "$Group.queue_autoscaler.min_replicas" not in script
    assert "pending_change=False state" in script


def test_flux_restore_normalizes_desired_replicas_with_separate_patch() -> None:
    script = RESTORE.read_text(encoding="utf-8")

    assert "normalizing desired replicas=" in script
    assert '-Body (@{ replicas = 0 } | ConvertTo-Json -Compress)' in script
    assert "desired replica normalization did not settle" in script
    assert "$RestoreBody" not in script


def test_controlled_image_runners_preserve_primary_failure_through_cleanup() -> None:
    for path in (PHASE4, PHASE6):
        script = path.read_text(encoding="utf-8")
        assert "$PrimaryFailure = $null" in script
        assert "$CleanupFailures = @()" in script
        assert "$PrimaryFailure = $_" in script
        assert "preserving the original Phase" in script
        assert "throw $PrimaryFailure" in script
