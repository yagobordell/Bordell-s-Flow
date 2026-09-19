from pathlib import Path

REPAIR_SCRIPT = Path("scripts/repair_salad_queue_attachment.ps1")


def test_queue_attachment_verifies_without_enabling_networking() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "queue_connection.queue_name" in script
    assert "queue_autoscaler.min_replicas" in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script


def test_zero_replica_group_can_repair_autoscaler_in_place() -> None:
    script = REPAIR_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$AllowMissing" in script
    assert "has no existing container group; preflight repair not needed" in script
    assert "Cancel pending jobs and allow " in script
    assert "running jobs to finish before changing the container group." in script
    assert "Terminal queue history will" in script
    assert "function Repair-GroupConfiguration" in script
    assert "Repairing Job Queue autoscaler in place" in script
    assert "queue_connection = New-QueueConnection" not in script
    assert "queue_autoscaler = New-QueueAutoscaler" in script
    assert "-Method Patch" in script
    assert "-Method Delete" not in script
    assert "missing or incorrect immutable queue_connection" in script
    assert "does not support changing queue_connection by PATCH" in script
    assert "function Wait-ForQueueAssociation" in script
    assert '$Status -notin @("stopped", "running", "deploying")' in script
    assert "within $TimeoutMinutes minute(s); refusing GPU allocation or job submission." in script



DIAGNOSTIC_SCRIPT = Path("scripts/diagnose_salad_queue_binding.ps1")


def test_queue_binding_diagnostic_is_read_only_and_exposes_both_sides() -> None:
    script = DIAGNOSTIC_SCRIPT.read_text(encoding="utf-8")

    assert "-Method Get -Uri $GroupUrl" in script
    assert "-Method Get -Uri $QueueUrl" in script
    assert '"queue_connection"' in script
    assert '"queue_autoscaler"' in script
    assert '"container_groups"' in script
    assert "binding.attached" in script
    assert "DIAGNOSIS=control_plane_association_mismatch" in script
    assert '/log-entries' in script
    assert 'resource.labels.container_group_name' in script
    assert "Diagnostic complete; no Salad resources were mutated." in script
    assert '-Method Delete' not in script
    assert '-Method Patch' not in script
    assert '"/start"' not in script
    assert '"/stop"' not in script


def test_queue_binding_diagnostic_filters_queue_transport_logs() -> None:
    script = DIAGNOSTIC_SCRIPT.read_text(encoding="utf-8")

    assert "queue|salad|heartbeat|worker|connect|ready|error|grpc|transport" in script
    assert "SALAD_API_KEY" in script
    assert 'Write-Host $Headers["Salad-Api-Key"]' not in script

