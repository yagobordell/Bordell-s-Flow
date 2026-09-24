from pathlib import Path

WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")


def test_worker_prepare_can_create_missing_container_group() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "function Try-Get-Group" in script
    assert "function New-ContainerGroup" in script
    assert "Creating container group: $GroupName" in script
    assert "-Uri $ContainersBase" in script
    assert "autostart_policy = $AutostartPolicy" in script
    assert "restart_policy = $RestartPolicy" in script
    assert "liveness_probe = New-Probe -Probe $Stack.shared_liveness_probe" in script
    assert "scheduled_scaling_enabled = $false" in script
    assert "replicas = 0" in script
    assert "Run -Action Prepare first" in script


def test_worker_prepare_normalizes_unexpected_replicas_to_zero() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "function Ensure-PreparedZeroReplicas" in script
    assert "Forcing replicas back to zero before Prepare completes." in script
    assert "$Group = Ensure-PreparedZeroReplicas -Headers $Headers -Group $Group" in script
    assert "could not be normalized to zero replicas" in script


def test_worker_manager_supports_env_file_and_unattended_deployment() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert '[string]$EnvFile = ".env"' in script
    assert '. (Join-Path $PSScriptRoot "_env_file.ps1")' in script
    loader = Path("scripts/salad/_env_file.ps1").read_text(encoding="utf-8")
    assert "function Import-EnvFile" in loader
    assert "[switch]$NonInteractive" in script
    assert "$Name is missing and -NonInteractive was requested." in script
    assert "shared_required_environment" in script
    assert "shared_environment" in script
    assert "required_environment" in script


def test_worker_manager_keeps_manifest_runtime_environment_authoritative() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    environment_builder = script.split(
        "function Get-WorkerEnvironment", maxsplit=1
    )[1].split("function New-ContainerConfiguration", maxsplit=1)[0]
    assert "$Environment[$Property.Name] = [string]$Property.Value" in environment_builder
    assert "GetEnvironmentVariable(\n            $Property.Name" not in environment_builder
    assert "must not disable the Salad queue" in environment_builder


def test_worker_prepare_can_recreate_stopped_group_for_queue_rebind() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "[switch]$Recreate" in script
    assert "function Remove-StoppedContainerGroup" in script
    assert "must be stopped before -Recreate" in script
    assert "must have replicas=0 before -Recreate" in script
    assert "-Method Delete" in script
    assert "fresh Job Queue attachment" in script
    assert "$ExistingGroup = $null" in script
    assert "New-ContainerGroup" in script


def test_worker_recreate_preserves_or_accepts_pinned_image_before_delete() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    prepare = script.split('"Prepare" {', maxsplit=1)[1]
    assert "[string]$PinnedImage" in script
    assert "Using explicit pinned image: $ResolvedPinnedImage" in prepare
    assert "Reusing existing immutable image before any group recreation" in prepare
    assert "$ResolvedPinnedImage = [string]$ExistingGroup.container.image" in prepare
    assert "Remove-StoppedContainerGroup -Headers $Headers" in prepare
    assert prepare.index("$ResolvedPinnedImage =") < prepare.index(
        "Remove-StoppedContainerGroup -Headers $Headers"
    )
    assert "-PinnedImage $ResolvedPinnedImage" in prepare
    assert "Assert-PreparedGroup -Group $Group -PinnedImage $ResolvedPinnedImage" in prepare


def test_worker_create_retries_transient_name_conflict_after_delete() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "function Test-NameConflictFailure" in script
    assert "$CreateDeadline = (Get-Date).AddMinutes(5)" in script
    assert "name_conflict attempt $CreateAttempt" in script
    assert "Try-Get-Group -Headers $Headers" in script
    assert "Continuing with normal post-create validation." in script
    assert "refusing unbounded recreate retries" in script


def test_worker_update_does_not_patch_immutable_queue_connection() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    create_block = script.split("function New-ContainerGroup", maxsplit=1)[1].split(
        "function Update-ContainerGroup", maxsplit=1
    )[0]
    update_block = script.split("function Update-ContainerGroup", maxsplit=1)[1].split(
        "function Ensure-PreparedZeroReplicas", maxsplit=1
    )[0]

    assert "queue_connection = New-QueueConnectionConfiguration" in create_block
    assert "queue_connection = New-QueueConnectionConfiguration" not in update_block
    assert "queue_autoscaler = New-QueueAutoscalerConfiguration" in update_block
