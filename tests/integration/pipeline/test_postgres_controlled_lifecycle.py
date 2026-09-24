from pathlib import Path

PYTHON_RUNNERS = (
    Path("scripts/pipeline/run_phase4_assets.py"),
    Path("scripts/pipeline/run_phase5_audio.py"),
    Path("scripts/pipeline/run_phase5_alignment.py"),
    Path("scripts/pipeline/run_phase6_keyframes.py"),
    Path("scripts/pipeline/run_phase8_videos.py"),
    Path("scripts/pipeline/run_phase8_upscale.py"),
)

CONTROLLED = (
    Path("scripts/pipeline/_qwen_controlled.ps1"),
    Path("scripts/pipeline/run_phase5_audio_controlled.ps1"),
    Path("scripts/pipeline/run_phase5_alignment_controlled.ps1"),
    Path("scripts/pipeline/run_phase8_videos_controlled.ps1"),
    Path("scripts/pipeline/run_phase8_upscale_controlled.ps1"),
)


def test_production_runners_submit_through_postgres_not_salad_queue() -> None:
    for path in PYTHON_RUNNERS:
        script = path.read_text(encoding="utf-8")
        assert "PostgresJobQueueClient" in script, path
        assert "SaladJobQueueClient" not in script, path


def test_controlled_gpu_wrappers_start_and_stop_explicit_capacity() -> None:
    for path in CONTROLLED:
        script = path.read_text(encoding="utf-8")
        assert "manage_salad_worker.ps1" in script, path
        assert ('Action = "Start"' in script or "Invoke-Start -Service" in script), path
        assert ('Action = "Stop"' in script or "-Action Stop" in script), path
        assert "finally {" in script, path
        assert "cleanup_salad_queue.ps1" not in script, path
        assert "start_salad_optimized_prewarm.ps1" not in script, path
        assert "start_salad_scale_to_zero.ps1" not in script, path


def test_qwen_cache_is_checked_before_gpu_start() -> None:
    script = Path("scripts/pipeline/_qwen_controlled.ps1").read_text(encoding="utf-8")

    assert script.index("$CacheAuditScript") < script.index('Action = "Start"')
    assert script.index("$Misses -eq 0") < script.index("& $WorkerManager @Start")


def test_ltx_capacity_is_bounded_by_manifest_and_cache_misses() -> None:
    script = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert "$Services.services.ltx25.capacity.max_replicas" in script
    assert "[Math]::Min($Misses, $MaxReplicas)" in script
    assert 'Replicas = $ReplicaCount' in script
    assert script.index("$CacheAudit") < script.index('Action = "Start"')


def test_upscale_capacity_is_bounded_by_manifest_and_cache_misses() -> None:
    script = Path("scripts/pipeline/run_phase8_upscale_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert "$Services.services.realesrgan.capacity.max_replicas" in script
    assert "[Math]::Min($Misses, $MaxReplicas)" in script
    assert 'Replicas = $ReplicaCount' in script
