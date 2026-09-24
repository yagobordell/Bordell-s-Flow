from pathlib import Path

PRODUCTION_RUNNER = Path("scripts/pipeline/run_production.py")
R2_PREFLIGHT = Path("scripts/pipeline/check_r2_ready.py")
QUEUE_CLEANUP = Path("scripts/salad/cleanup_salad_queue.ps1")
QWEN_CONTROLLER = Path("scripts/pipeline/_qwen_controlled.ps1")
QWEN_WRAPPERS = (
    Path("scripts/pipeline/run_phase4_assets_controlled.ps1"),
    Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1"),
)
READY_BEFORE_QUEUE_RUNNERS = (
    Path("scripts/pipeline/run_phase4_assets_controlled.ps1"),
    Path("scripts/pipeline/run_phase5_audio_controlled.ps1"),
    Path("scripts/pipeline/run_phase5_alignment_controlled.ps1"),
    Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1"),
)
ALL_CONTROLLED_RUNNERS = READY_BEFORE_QUEUE_RUNNERS + (
    Path("scripts/pipeline/run_phase8_videos_controlled.ps1"),
)
GPU_CLIENTS = (
    Path("scripts/pipeline/run_phase4_assets.py"),
    Path("scripts/pipeline/run_phase5_audio.py"),
    Path("scripts/pipeline/run_phase5_alignment.py"),
    Path("scripts/pipeline/run_phase6_keyframes.py"),
    Path("scripts/pipeline/run_phase8_videos.py"),
)


def _lifecycle_source(path: Path) -> str:
    source = QWEN_CONTROLLER if path in QWEN_WRAPPERS else path
    return source.read_text(encoding="utf-8")


def test_controlled_gpu_runners_prewarm_and_always_stop() -> None:
    for path in ALL_CONTROLLED_RUNNERS:
        text = _lifecycle_source(path)
        assert "start_salad_optimized_prewarm.ps1" in text, path.name
        assert "finally" in text, path.name
        assert "-Action Stop" in text, path.name
        assert "-Action Status" in text, path.name
    for path in QWEN_WRAPPERS:
        assert "_qwen_controlled.ps1" in path.read_text(encoding="utf-8")


def test_qwen_controlled_runners_pin_warm_and_clean_queue() -> None:
    assert QUEUE_CLEANUP.is_file()
    cleanup = QUEUE_CLEANUP.read_text(encoding="utf-8")
    assert "foreach ($Job in $ActiveJobs)" in cleanup
    assert "Cancelling abandoned active job after group stop" in cleanup
    assert "Waiting for cancelled running job(s)" in cleanup
    assert "requires '$GroupName' fully stopped first" in cleanup

    text = QWEN_CONTROLLER.read_text(encoding="utf-8")
    assert "HoldReadyReplica = $true" in text
    assert "hold_salad_warm_replica.ps1" not in text
    assert "cleanup_salad_queue.ps1" in text
    assert text.index("-Action Stop") < text.index("& $QueueCleanup")
    assert text.index("& $QueueCleanup") < text.index("-Action Status")
    assert text.index("if ($Misses -eq 0 -and $Hits -eq $Plan.Count)") < text.index(
        "$PrewarmArguments = @{"
    )


def test_controlled_gpu_runners_preflight_r2_before_prewarm() -> None:
    assert R2_PREFLIGHT.is_file()
    for path in ALL_CONTROLLED_RUNNERS:
        text = _lifecycle_source(path)
        assert "check_r2_ready.py" in text, path.name
        assert "R2 preflight failed; refusing to allocate" in text, path.name
        prewarm_call = (
            "Invoke-Prewarm -Service breeze_tts2"
            if path.name == "run_phase5_audio_controlled.ps1"
            else "& $OptimizedPrewarm"
        )
        assert text.index("& python $R2Preflight") < text.index(prewarm_call), path.name


def test_gpu_clients_use_bounded_r2_client() -> None:
    for path in GPU_CLIENTS:
        text = path.read_text(encoding="utf-8")
        assert "from ai_video_factory.providers.r2 import create_r2_storage" in text, path.name
        assert "R2ObjectStorage.create(" not in text, path.name


def test_ready_before_queue_runners_keep_short_pending_deadline() -> None:
    for path in READY_BEFORE_QUEUE_RUNNERS:
        text = path.read_text(encoding="utf-8")
        assert "pending-timeout-seconds" in text, path.name


def test_phase5_clients_have_separate_pending_deadline() -> None:
    for path in (
        Path("scripts/pipeline/run_phase5_audio.py"),
        Path("scripts/pipeline/run_phase5_alignment.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert '"--pending-timeout-seconds"' in text
        assert "pending_timeout_seconds=args.pending_timeout_seconds" in text


def test_full_production_routes_every_gpu_stage_through_controlled_runner() -> None:
    text = PRODUCTION_RUNNER.read_text(encoding="utf-8")

    for script in ALL_CONTROLLED_RUNNERS:
        assert script.as_posix() in text
    for stage_name in (
        "phase4-reference-assets",
        "phase5-narration",
        "phase5-alignment",
        "phase6-keyframes",
        "phase8-videos",
    ):
        assert f'stage_name == "{stage_name}"' in text
    assert '"powershell.exe" if os.name == "nt" else "pwsh"' in text
