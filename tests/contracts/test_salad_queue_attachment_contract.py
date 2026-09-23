from pathlib import Path

REPAIR_SCRIPT = Path("scripts/salad/repair_salad_queue_attachment.ps1")


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
    assert "function Wait-ForQueueAssociation" not in script
    assert "non-authoritative for zero-replica/stopped workers" in script
    assert "queue_connection and real " in script
    assert "transport execution are the acceptance signals." in script



DIAGNOSTIC_SCRIPT = Path("scripts/diagnostics/diagnose_salad_queue_binding.ps1")


def test_queue_binding_diagnostic_is_read_only_and_exposes_both_sides() -> None:
    script = DIAGNOSTIC_SCRIPT.read_text(encoding="utf-8")

    assert "-Method Get -Uri $GroupUrl" in script
    assert "-Method Get -Uri $QueueUrl" in script
    assert '"queue_connection"' in script
    assert '"queue_autoscaler"' in script
    assert '"container_groups"' in script
    assert "binding.attached" in script
    assert "group.environment.SALAD_LOG_LEVEL=" in script
    assert "DIAGNOSIS=queue_listing_non_authoritative_routing_proven" in script
    assert "DIAGNOSIS=queue_listing_absent_runtime_unproven" in script
    assert '/log-entries' in script
    assert 'resource.labels.container_group_name' in script
    assert "Diagnostic complete; no Salad resources were mutated." in script
    assert '-Method Delete' not in script
    assert '-Method Patch' not in script
    assert '"/start"' not in script
    assert '"/stop"' not in script


def test_queue_binding_diagnostic_filters_queue_transport_logs() -> None:
    script = DIAGNOSTIC_SCRIPT.read_text(encoding="utf-8")

    assert "queue|salad|worker|connect|ready|error|grpc|transport" in script
    assert "exception|traceback|cuda|oom|out of memory" in script
    assert "shape|dimension|frame|invalid|failed|job execution" in script
    assert "SALAD_API_KEY" in script
    assert 'Write-Host $Headers["Salad-Api-Key"]' not in script



def test_queue_binding_diagnostic_shows_recent_queue_job_history() -> None:
    script = DIAGNOSTIC_SCRIPT.read_text(encoding="utf-8")

    assert "=== Recent queue jobs ===" in script
    assert '"$QueueUrl/jobs?page=1&page_size=25"' in script
    assert "Select-Object id, status, create_time, update_time" in script
    assert "$RecentJobs = @(@($Jobs.items) | Select-Object -First 25)" in script
    assert "$SucceededJobCount" in script
    assert "=== Failed queue job details ===" in script
    assert "job.events=" in script
    assert "job.output=" in script
    assert "Join-String" not in script
    assert ') -join ","' in script
