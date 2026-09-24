from pathlib import Path

BOOTSTRAP = Path("scripts/salad/start_salad_protected_smoke.ps1")
PREPARE = Path("scripts/smoke/prepare_parallel_benchmarks.ps1")
PREPARED = Path("scripts/smoke/_salad_prepared_benchmark_worker.ps1")
AVAILABILITY = Path("scripts/salad/check_salad_gpu_availability.ps1")
QWEN = Path("scripts/smoke/benchmark_qwen_image_21.ps1")
LTX = Path("scripts/smoke/benchmark_ltx25_a2v.ps1")


def test_parallel_prepare_checks_both_services_before_any_upgrade() -> None:
    content = PREPARE.read_text(encoding="utf-8")
    assert '@("qwen_image_21", "ltx25")' in content
    assert content.index("foreach ($Name in $Services)") < content.index(
        "foreach ($Name in $Services)", content.index("foreach ($Name in $Services)") + 1
    )
    assert "pending_change" in content
    assert "replicas -ne 0" in content
    assert "Resolve-SaladBenchmarkPinnedImage" in content
    assert '$Options["SkipBuild"] = $true' in content
    assert '$Options["PinnedImage"] = $Pinned' in content
    assert '"Prepare"' in content
    assert '"Start"' not in content
    assert "skipping Docker build and Salad upgrade" in content


def test_prepared_image_guard_requires_current_immutable_digest() -> None:
    content = PREPARED.read_text(encoding="utf-8")
    assert "docker buildx imagetools inspect $Image" in content
    assert "sha256:[0-9a-f]{64}" in content
    assert 'if ([string]$Group.container.image -ne $PinnedImage)' in content
    assert "$Group.queue_connection.queue_name" in content
    assert "pending_change" in content
    assert "[int]$Group.replicas -ne 0" in content
    assert "queue_autoscaler.min_replicas=0" in content


def test_benchmarks_use_distinct_services_with_prepared_mode_and_final_stop() -> None:
    for file, service in ((QWEN, "qwen_image_21"), (LTX, "ltx25")):
        content = file.read_text(encoding="utf-8")
        assert "[switch]$UsePreparedImage" in content
        assert "$AllocatingTimeoutMinutes = 10" in content
        assert "Assert-SaladPreparedBenchmarkWorker" in content
        assert "-AllocatingTimeoutMinutes $AllocatingTimeoutMinutes" in content
        assert f'Service = "{service}"' in content
        assert "-Action Stop" in content
        assert "finally {" in content
        assert 'if ($UsePreparedImage) {' in content


def test_gpu_availability_uses_actual_manifest_cpu_ram_and_storage() -> None:
    content = AVAILABILITY.read_text(encoding="utf-8")
    assert '"$BaseUrl/gpu-classes"' in content
    assert '"$BaseUrl/availability/sce-gpu-availability"' in content
    assert "gpu_classes = $ClassIds" in content
    assert "cpu = [int]$Definition.resources.cpu" in content
    assert "memory = [int]$Definition.resources.memory" in content
    assert "storage_amount = [long]$Definition.resources.storage_amount" in content
    assert "available_gpu_high" in content
    assert "Write-Warning" in content
    assert "reallocate" not in content.lower()


def test_allocating_deadline_is_not_reset_when_salad_changes_unstarted_instance_id() -> None:
    content = BOOTSTRAP.read_text(encoding="utf-8")
    allocation = content.split("$WaitingForGpuAllocation = (", 1)[1].split(
        "if (\n        $ReallocationPending", 1
    )[0]
    assert "$StartedInstances.Count -eq 0" in allocation
    assert "($Status -eq \"running\" -and $Instances.Count -eq 0)" in allocation
    assert "if ($null -eq $AllocatingSince)" in allocation
    assert "$InstanceId -ne" not in allocation
    assert "$AllocatingElapsed.TotalMinutes -ge $AllocatingTimeoutMinutes" in allocation


def test_parallel_prepare_checks_queue_before_mutating_either_group() -> None:
    script = PREPARE.read_text(encoding="utf-8")
    summary_check = script.index("queue reports current_queue_length=")
    replica_repair = script.index('& $Manager -Service $Name -Action Stop')
    image_prepare = script.index('$Options = @{ Service = $Name; Action = "Prepare"')
    assert summary_check < replica_repair < image_prepare
    assert '$Queue.PSObject.Properties.Name -notcontains "current_queue_length"' in script
    assert "inspect_salad_queue_state.ps1 -Service $Name" in script
    assert "Invoke-RestMethod -Method Get" in script
