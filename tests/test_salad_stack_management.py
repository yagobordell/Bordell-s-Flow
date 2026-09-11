import json
import re
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
WORKER_MANAGER = Path("scripts/manage_salad_worker.ps1")
STACK_MANAGER = Path("scripts/manage_salad_stack.ps1")
QUEUE_REPAIR = Path("scripts/repair_salad_queue_attachment.ps1")


def _document() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_salad_manifest_centralizes_stack_identity_and_shared_environment() -> None:
    document = _document()

    assert document["schema_version"] == "2"
    assert document["stack"]["organization"] == "yagobordellorg"
    assert document["stack"]["project"] == "aivideofactory"
    assert document["stack"]["service_order"] == [
        "whisper",
        "breeze_tts2",
        "ideogram4",
        "ltx25",
    ]
    assert document["stack"]["shared_required_environment"] == [
        "POSTGRES_DSN",
        "R2_ENDPOINT_URL",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ]

    for service in document["services"].values():
        assert "organization" not in service
        assert "project" not in service
        assert "required_secrets" not in service
        assert service["autoscaler"]["min_replicas"] == 0


def test_every_model_has_its_own_group_and_queue() -> None:
    services = _document()["services"]
    groups = [service["group_name"] for service in services.values()]
    queues = [service["queue_name"] for service in services.values()]

    assert len(groups) == len(set(groups)) == 4
    assert len(queues) == len(set(queues)) == 4
    assert services["whisper"]["required_environment"] == []
    assert services["breeze_tts2"]["required_environment"] == []
    assert services["breeze_tts2"]["group_name"] == "ai-video-factory-breeze-tts2-worker-v2"
    assert services["ideogram4"]["required_environment"] == ["HF_TOKEN"]
    assert services["ltx25"]["required_environment"] == ["HF_TOKEN"]


def test_worker_queue_display_names_match_salad_api_contract() -> None:
    display_name_pattern = re.compile(r"^[ A-Za-z0-9,.\-]{2,63}$")
    services = _document()["services"]

    for service in services.values():
        assert display_name_pattern.fullmatch(service["display_name"])
        assert display_name_pattern.fullmatch(f'{service["display_name"]} Jobs')

    script = WORKER_MANAGER.read_text(encoding="utf-8")
    assert 'display_name = "$($Definition.display_name) Jobs"' in script
    assert 'display_name = "$Service jobs"' not in script


def test_worker_prepare_can_create_missing_container_group() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "function Try-Get-Group" in script
    assert "function New-ContainerGroup" in script
    assert "Creating container group: $GroupName" in script
    assert "-Uri $ContainersBase" in script
    assert "autostart_policy = $AutostartPolicy" in script
    assert "restart_policy = $RestartPolicy" in script
    assert "scheduled_scaling_enabled = $false" in script
    assert "replicas = 0" in script
    assert "Run -Action Prepare first" in script


def test_worker_manager_supports_env_file_and_unattended_deployment() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert '[string]$EnvFile = ".env"' in script
    assert "function Import-EnvFile" in script
    assert "[switch]$NonInteractive" in script
    assert "$Name is missing and -NonInteractive was requested." in script
    assert "shared_required_environment" in script
    assert "required_environment" in script


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
    assert "Cancel them before Prepare can change the container group." in script
    assert 'current_state.status -ne "stopped"' in script
    assert "function Set-ZeroReplicas" in script
    assert "function Repair-GroupConfiguration" in script
    assert "Repairing Job Queue autoscaling in place" in script
    assert "Normalizing stopped group" in script
    assert '@{ replicas = 0 }' in script
    assert "function New-Networking" not in script
    assert "networking = New-Networking" not in script
    assert "queue_connection = New-QueueConnection" in script
    assert "queue_autoscaler = New-QueueAutoscaler" in script
    assert "-Method Patch" in script
    assert "-Method Delete" not in script
    assert "Recreating stopped container group" not in script
    assert "Runtime attachment will be validated after Start/Smoke." in script
    assert "Test-QueueAttachment" in script
