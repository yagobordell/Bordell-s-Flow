from pathlib import Path

WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")
STACK_MANAGER = Path("scripts/salad/manage_salad_stack.ps1")
QUEUE_REPAIR = Path("scripts/salad/repair_salad_queue_attachment.ps1")


def test_stack_manager_orchestrates_all_model_services() -> None:
    script = STACK_MANAGER.read_text(encoding="utf-8")

    assert 'ValidateSet("Validate", "Prepare", "Start", "Status", "Stop")' in script
    assert "$Document.stack.service_order" in script
    assert "Every model service must have its own Salad container group." in script
    assert "Every model service must have its own Salad job queue." in script
    assert '[array]::Reverse($ExecutionOrder)' in script
    assert '& $WorkerManager @WorkerArguments' in script
    assert '$WorkerArguments = @{' in script
    assert 'Service = $Name' in script
    assert 'Action = $WorkerAction' in script
    assert '"-Service", $Name' not in script
    assert 'Get-Setting -Name "SALAD_API_KEY"' in script
    assert "start_salad_scale_to_zero.ps1" in script


def test_prepare_repairs_queue_attachment_before_and_after_worker_update() -> None:
    script = STACK_MANAGER.read_text(encoding="utf-8")

    prepare_block = script.split('"Prepare" {', maxsplit=1)[1].split('"Start" {', maxsplit=1)[0]
    assert "Invoke-QueueRepair -Name $Name -AllowMissing" in prepare_block
    assert 'Invoke-WorkerAction -Name $Name -WorkerAction "Prepare"' in prepare_block
    assert "Invoke-QueueRepair -Name $Name" in prepare_block
    assert prepare_block.index("-AllowMissing") < prepare_block.index('-WorkerAction "Prepare"')


def test_queue_repair_patches_autoscaling_without_reusing_group_name() -> None:
    script = QUEUE_REPAIR.read_text(encoding="utf-8")

    assert "[switch]$AllowMissing" in script
    assert "current_queue_length" in script
    assert "function Get-ActiveQueueJobs" in script
    assert "Cancel pending jobs and allow " in script
    assert "running jobs to finish before changing the container group." in script
    assert "Terminal queue history will" in script
    assert '$GroupStatus -notin @("stopped", "running", "deploying")' in script
    assert "function Set-ZeroReplicas" in script
    assert "function Repair-GroupConfiguration" in script
    assert "Repairing Job Queue autoscaler in place" in script
    assert "Normalizing group" in script
    assert "$Updated = Set-ZeroReplicas -Group $Updated" in script
    assert '@{ replicas = 0 }' in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script
    assert "queue_connection = New-QueueConnection" not in script
    assert "queue_autoscaler = New-QueueAutoscaler" in script
    assert "-Method Patch" in script
    assert "-Method Delete" not in script
    assert "Recreating stopped container group" not in script
    assert "Wait-ForQueueAssociation" not in script
    assert "non-authoritative for zero-replica/stopped workers" in script
    assert "Test-QueueAttachment" in script


def test_stack_prepare_forwards_targeted_recreate() -> None:
    script = STACK_MANAGER.read_text(encoding="utf-8")

    assert "[switch]$Recreate" in script
    assert '$WorkerArguments["Recreate"] = $true' in script
    assert '-Recreate is only valid with -Action Prepare.' in script


def test_stack_manager_forwards_explicit_pinned_image_only_to_targeted_service() -> None:
    script = STACK_MANAGER.read_text(encoding="utf-8")

    assert "[string]$PinnedImage" in script
    assert '$WorkerArguments["PinnedImage"] = $PinnedImage' in script
    assert "-PinnedImage requires exactly one selected service." in script
