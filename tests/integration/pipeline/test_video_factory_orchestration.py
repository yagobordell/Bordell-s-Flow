from pathlib import Path

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file()
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_one_command_runner_requires_global_capacity_controller() -> None:
    text = _read("scripts/pipeline/run_video_factory.ps1")

    preflight = text.index("VIDEO FACTORY PREFLIGHT")
    capacity = text.index("SALAD CAPACITY")
    production = text.index("VIDEO FACTORY DAG")
    phase9 = text.index("PHASE 9")
    assert preflight < capacity < production < phase9
    assert "manage_salad_stack.ps1" in text
    assert "Assert-CapacityControllerHealthy" in text
    assert "--check-health" in text
    assert 'SALAD_AUTOSCALER_ENABLED = "true"' in text
    assert "Start-CapacityController" not in text
    assert "Stop-CapacityController" not in text
    assert "Invoke-FinalCleanup" not in text
    assert "global controller owns shared scale-to-zero" in text
    assert "cleanup_salad_queue.ps1" not in text
    assert "Manual intervention: 0" in text

def test_one_command_runner_avoids_powershell_automatic_input_collision() -> None:
    text = _read("scripts/pipeline/run_video_factory.ps1")

    assert '[Alias("Input")]' in text
    assert "[string]$ScriptFile" in text
    assert "$ResolvedInput = Resolve-InputPath -Path $ScriptFile" in text
    assert "[string]$Input" not in text


def test_phase5_checks_breeze_cache_before_primary_gpu_start() -> None:
    text = _read("scripts/pipeline/run_phase5_audio_controlled.ps1")

    audit = text.index("Phase 5 cache plan")
    start = text.index("Invoke-Start -Service breeze_tts2")
    assert audit < start
    assert "no Breeze or Fish GPU allocation required" in text
    assert "Fish consumed zero GPU-seconds" in text


def test_phase4_and_phase6_use_qwen_only() -> None:
    shared = _read("scripts/pipeline/_qwen_controlled.ps1")
    assert 'Service = "qwen_image_21"' in shared
    assert "ideogram" not in shared.lower()
    for controlled, runner in (
        (
            "scripts/pipeline/run_phase4_assets_controlled.ps1",
            "scripts/pipeline/run_phase4_assets.py",
        ),
        (
            "scripts/pipeline/run_phase6_keyframes_controlled.ps1",
            "scripts/pipeline/run_phase6_keyframes.py",
        ),
    ):
        controlled_text = _read(controlled)
        runner_text = _read(runner)
        assert "_qwen_controlled.ps1" in controlled_text
        assert "SaladQwenImage21Provider" in runner_text
        assert "PostgresJobQueueClient" in runner_text
        assert "QWEN_IMAGE_21_" in runner_text
        assert "ideogram" not in controlled_text.lower()
        assert "ideogram" not in runner_text.lower()


def test_qwen_controlled_runners_start_capacity_after_cache_plan() -> None:
    shared = _read("scripts/pipeline/_qwen_controlled.ps1")

    assert shared.index("$CacheAuditScript") < shared.index('Action = "Start"')
    assert shared.index('Action = "Start"') < shared.rindex(
        "& python $RunnerScript @RunnerArguments"
    )
    assert "manage_salad_worker.ps1" in shared
    assert "start_salad_optimized_prewarm.ps1" not in shared
    assert "cleanup_salad_queue.ps1" not in shared
    assert 'Action = "Stop"; Service = "qwen_image_21"' in shared


def test_phase8_checks_r2_replay_before_ltx_capacity() -> None:
    text = _read("scripts/pipeline/run_phase8_videos_controlled.ps1")

    audit = text.index("$CacheAudit")
    start = text.index('Action = "Start"; Service = "ltx25"')
    assert audit < start
    assert "All Phase 8 clips are cached; no LTX GPU allocation required." in text


def test_phase5_alignment_checks_cache_before_whisper_capacity() -> None:
    text = _read("scripts/pipeline/run_phase5_alignment_controlled.ps1")

    audit = text.index("$CacheAudit")
    start = text.index('Action = "Start"; Service = "whisper"')
    assert audit < start
    assert "Whisper alignment is cached; no GPU allocation required." in text


def test_one_command_runner_exposes_narration_language_and_defaults_to_english() -> None:
    text = _read("scripts/pipeline/run_video_factory.ps1")
    production = _read("scripts/pipeline/run_production.py")
    controlled = _read("scripts/pipeline/run_phase5_alignment_controlled.ps1")

    assert '[string]$NarrationLanguage = "en"' in text
    assert '"--narration-language"' in text
    assert "$NarrationLanguage" in text
    assert "--narration-language" in production
    assert 'default="en"' in production
    assert '[string]$Language = "en"' in controlled
    assert "--language" in controlled
    assert "$Language" in controlled


def test_phase5_alignment_cache_audit_receives_same_explicit_language() -> None:
    text = _read("scripts/pipeline/run_phase5_alignment_controlled.ps1")

    assert "& python $CacheAudit --source $Source --audio $Audio --language $Language" in text
    runner_block = text.split("$RunnerArguments = @(", maxsplit=1)[1].split(
        ")", maxsplit=1
    )[0]
    assert '"--language", $Language' in runner_block


def test_phase9_plan_removes_stale_artifact_before_rebuild() -> None:
    text = _read("scripts/pipeline/run_phase9_compositor.py")

    assert "args.output.unlink(missing_ok=True)" in text
    assert text.index("args.output.unlink(missing_ok=True)") < text.index(
        "plan = build_composition_plan("
    )


def test_one_command_runner_isolates_each_run_and_serializes_shared_renderer() -> None:
    text = _read("scripts/pipeline/run_video_factory.ps1")

    assert "[string]$RunId" in text
    assert 'data\\output\\runs' in text
    assert 'data\\tmp\\runs' in text
    assert "$env:OUTPUT_DIR = $OutputDir" in text
    assert "$env:TEMP_DIR = $TempDir" in text
    assert '"--output-dir"' in text
    assert "BordellsFlow-Phase9-Renderer" in text
    assert "Enter-NamedMutex" in text
    assert "Exit-NamedMutex" in text
