from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_one_command_runner_preflights_before_production_and_always_cleans_up() -> None:
    text = _read("scripts/run_video_factory.ps1")

    preflight = text.index("VIDEO FACTORY PREFLIGHT")
    production = text.index("VIDEO FACTORY DAG")
    phase9 = text.index("PHASE 9")
    cleanup = text.index("FINAL CLEANUP")
    assert preflight < production < phase9 < cleanup
    assert "manage_salad_stack.ps1" in text
    assert "-Action Stop" in text
    assert "cleanup_salad_queue.ps1" in text
    assert "Manual intervention: 0" in text


def test_one_command_runner_avoids_powershell_automatic_input_collision() -> None:
    text = _read("scripts/run_video_factory.ps1")

    assert '[Alias("Input")]' in text
    assert "[string]$ScriptFile" in text
    assert "$ResolvedInput = Resolve-InputPath -Path $ScriptFile" in text
    assert "[string]$Input" not in text


def test_phase5_checks_breeze_cache_before_primary_prewarm() -> None:
    text = _read("scripts/run_phase5_audio_controlled.ps1")

    audit = text.index("Phase 5 cache plan")
    prewarm = text.index("Breeze prewarm: exactly one ready primary replica")
    assert audit < prewarm
    assert "no Breeze or Fish GPU allocation required" in text
    assert "Fish consumed zero GPU-seconds" in text


def test_flux_dynamic_fallback_prewarms_on_first_confirmed_safety_rejection() -> None:
    phase4 = _read("scripts/run_phase4_assets_controlled.ps1")
    phase6 = _read("scripts/run_phase6_keyframes_controlled.ps1")
    phase4_runner = _read("scripts/run_phase4_assets.py")
    phase6_runner = _read("scripts/run_phase6_keyframes.py")

    for text in (phase4, phase6):
        assert "--prewarm-fallback-on-demand" in text
        assert '"start_salad_scale_to_zero.ps1"' not in text
        assert "$OnDemandFluxPrewarm = $IdeogramNeeded -and -not $FluxNeeded" in text

    for text in (phase4_runner, phase6_runner):
        assert "Ideogram safety rejection confirmed; prewarming FLUX" in text
        assert '"start_salad_flux_prewarm.ps1"' in text


def test_phase6_checks_cache_before_ideogram_and_does_not_eager_prewarm_flux() -> None:
    text = _read("scripts/run_phase6_keyframes_controlled.ps1")

    audit = text.index("Phase 6 cache plan")
    ideogram = text.index("Ideogram optimized prewarm")
    flux = text.index("FLUX fallback: prewarm only because cached safety evidence requires it")
    assert audit < ideogram
    assert audit < flux
    assert "--prewarm-fallback-on-demand" in text
    assert "if ($FluxNeeded)" in text


def test_queue_cleanup_cancels_orphaned_active_jobs_after_group_stop() -> None:
    text = _read("scripts/cleanup_salad_queue.ps1")

    assert "Cancelling abandoned active job after group stop" in text
    assert "foreach ($Job in $ActiveJobs)" in text
    assert "Get-HttpStatusCode" in text


def test_phase8_checks_r2_replay_before_ltx_prewarm() -> None:
    text = _read("scripts/run_phase8_videos_controlled.ps1")

    audit = text.index("Phase 8 cache plan")
    prewarm = text.index("LTX optimized prewarm")
    assert audit < prewarm
    assert "All Phase 8 clips are valid R2 replays" in text


def test_shared_ideogram_hold_is_explicitly_bounded_to_end_to_end_mode() -> None:
    production = _read("scripts/run_production.py")
    phase4 = _read("scripts/run_phase4_assets_controlled.ps1")
    phase6 = _read("scripts/run_phase6_keyframes_controlled.ps1")

    assert 'arguments.append("-KeepIdeogramWarm")' in production
    assert 'arguments.append("-ReleaseSharedIdeogram")' in production
    assert "[switch]$KeepIdeogramWarm" in phase4
    assert "[switch]$ReleaseSharedIdeogram" in phase6
    assert "outer orchestration owns cleanup" in phase4


def test_phase5_alignment_checks_cache_before_whisper_prewarm() -> None:
    text = _read("scripts/run_phase5_alignment_controlled.ps1")

    audit = text.index("Phase 5 alignment cache")
    prewarm = text.index("Whisper optimized prewarm")
    assert audit < prewarm
    assert "no Whisper GPU allocation required" in text


def test_one_command_runner_exposes_narration_language_and_defaults_to_english() -> None:
    text = _read("scripts/run_video_factory.ps1")
    production = _read("scripts/run_production.py")
    controlled = _read("scripts/run_phase5_alignment_controlled.ps1")

    assert '[string]$NarrationLanguage = "en"' in text
    assert '"--narration-language"' in text
    assert "$NarrationLanguage" in text
    assert '--narration-language' in production
    assert 'default="en"' in production
    assert '[string]$Language = "en"' in controlled
    assert "--language" in controlled
    assert "$Language" in controlled


def test_phase5_alignment_cache_audit_receives_same_explicit_language() -> None:
    text = _read("scripts/run_phase5_alignment_controlled.ps1")

    audit_block = text.split("Phase 5 alignment cache", maxsplit=1)[1].split(
        "$CachePlan =", maxsplit=1
    )[0]
    runner_block = text.split("$RunnerArguments = @(", maxsplit=1)[1].split(
        ")", maxsplit=1
    )[0]

    assert "--language $Language" in audit_block
    assert '"--language", $Language' in runner_block


def test_phase9_plan_removes_stale_artifact_before_rebuild() -> None:
    text = _read("scripts/run_phase9_compositor.py")

    assert "args.output.unlink(missing_ok=True)" in text
    assert text.index("args.output.unlink(missing_ok=True)") < text.index(
        "plan = build_composition_plan("
    )
