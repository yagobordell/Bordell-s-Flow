import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")


def test_breeze_uses_high_priority_for_current_capacity() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["services"]["breeze_tts2"]["priority"] == "high"


def test_breeze_uses_indexed_cuda_device() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["services"]["breeze_tts2"]["environment"]["BREEZE_DEVICE"] == "cuda:0"


def test_worker_update_sends_priority_inside_container() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")
    update_block = script.split("function Update-ContainerGroup", maxsplit=1)[1].split(
        "function Assert-PreparedGroup", maxsplit=1
    )[0]
    container_block = script.split("function New-ContainerConfiguration", maxsplit=1)[1].split(
        "function Test-LegacyQueueAttachment", maxsplit=1
    )[0]

    assert "container = New-ContainerConfiguration" in update_block
    assert "priority = [string]$Definition.priority" in container_block


def test_prepare_rejects_a_priority_mismatch() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert "[string]$Group.container.priority -ne [string]$Definition.priority" in script
    assert "Salad did not activate the expected container priority" in script


def test_ltx_uses_high_priority_for_rtx5090_capacity() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert document["services"]["ltx25"]["priority"] == "high"
