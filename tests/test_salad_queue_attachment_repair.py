from pathlib import Path

REPAIR = Path("scripts/repair_salad_queue_attachment.ps1")
STACK = Path("scripts/manage_salad_stack.ps1")


def test_queue_attachment_repair_contract() -> None:
    text = REPAIR.read_text(encoding="utf-8")
    assert "queue_autoscaler" in text
    assert "queue_connection" in text
    assert "networking" in text
    assert "current_queue_length" in text
    assert "Method Delete" not in text
    assert "Method Patch" in text
    assert "replicas -ne 0" in text
    assert "Repair-GroupConfiguration" in text
    assert "Forcing replicas back to zero now." in text
    assert "$Updated = Set-ZeroReplicas -Group $Updated" in text
    assert "Runtime attachment will be validated after Start/Smoke." in text
    assert "did not persist the complete Job Queue autoscaling configuration after PATCH" in text


def test_queue_attachment_preflight_allows_missing_queue() -> None:
    text = REPAIR.read_text(encoding="utf-8")
    assert "function Try-Get-Queue" in text
    assert "(Get-HttpStatusCode -ErrorRecord $_) -eq 404" in text
    assert "$Queue = Try-Get-Queue" in text
    assert 'if ($AllowMissing)' in text
    assert "has no existing job queue; preflight repair not needed." in text
    assert "Job queue '$QueueName' is missing. Run Prepare first." in text


def test_queue_attachment_preflight_always_enumerates_active_jobs() -> None:
    text = REPAIR.read_text(encoding="utf-8")
    assert "function Get-ActiveQueueJobs" in text
    assert 'page_size=$PageSize' in text
    assert "$PageSize = 100" in text
    assert '@("pending", "running")' in text
    assert "$ActiveJobs = @(Get-ActiveQueueJobs)" in text
    assert "if ($ActiveJobs.Count -ne 0)" in text
    assert "Write-ActiveQueueJobs -Jobs $ActiveJobs" in text
    assert "active transport={0} status={1} application={2}" in text
    assert "contains $($ActiveJobs.Count) active job(s)" in text
    assert "exhaustive pagination found no pending/running jobs" in text
    assert "Terminal queue history will" in text


def test_stack_prepare_runs_queue_attachment_repair() -> None:
    text = STACK.read_text(encoding="utf-8")
    assert "repair_salad_queue_attachment.ps1" in text
    assert '$Action -eq "Prepare"' in text
    assert "Invoke-QueueRepair -Name $Name -AllowMissing" in text
    assert "Invoke-WorkerAction -Name $Name -WorkerAction \"Prepare\"" in text
    assert "Invoke-QueueRepair -Name $Name" in text
