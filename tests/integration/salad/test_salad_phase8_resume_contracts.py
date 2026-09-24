from pathlib import Path


def test_phase8_resume_starts_scale_to_zero_group_only_for_active_current_jobs() -> None:
    text = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert "video_generation_manifest.json" in text
    assert "inspect_phase8_manifest.py" in text
    assert '[string]$ManifestState.status -eq "matching"' in text
    assert "[int]$ManifestState.active_resume_jobs -gt 0" in text
    assert "start_salad_scale_to_zero.ps1" in text
    assert 'Service = "ltx25"' in text
    assert "if ($ResumeSubmittedJobs)" in text
    assert "manage_salad_worker.ps1" not in text
    assert "start_salad_optimized_prewarm.ps1" in text


def test_phase8_controlled_runner_pins_canonical_salad_route() -> None:
    text = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert "deploy\\salad\\services.json" in text
    assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
    assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
    assert "$env:SALAD_LTX25_QUEUE_NAME = [string]$LtxService.queue_name" in text
    assert "Phase 8 canonical Salad route" in text


def test_phase8_resume_detection_happens_before_gpu_allocation() -> None:
    text = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert text.index("$ManifestPath = Join-Path $OutputDir") < text.index(
        "=== R2 preflight: verify storage before GPU allocation ==="
    )
    assert text.index("& python $ManifestInspector") < text.index(
        "=== R2 preflight: verify storage before GPU allocation ==="
    )
    assert text.index("if ($ResumeSubmittedJobs)") < text.index(
        "=== Phase 8 video generation: worker group available; resume/fanout active ==="
    )


def test_phase8_resume_hard_guards_queue_before_scale_to_zero_start() -> None:
    text = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert "check_salad_queue_ready.py" in text
    assert "& python $QueueGuard ltx25 --output-dir $ProductionOutputRoot" in text
    assert "LTX resume queue ownership guard failed; refusing GPU allocation." in text
    assert text.index(
        "& python $QueueGuard ltx25 --output-dir $ProductionOutputRoot"
    ) < text.index("& $ScaleToZeroStarter @StartArguments")


def test_phase8_archives_stale_manifest_only_after_idle_queue_guard() -> None:
    text = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    stale_branch = text.index(
        'if ([string]$ManifestState.status -eq "different_plan")'
    )
    idle_guard = text.index("& python $QueueGuard ltx25 --output-dir $EmptyGuardRoot")
    archive = text.index("--archive-mismatch", stale_branch)
    r2_preflight = text.index(
        "=== R2 preflight: verify storage before GPU allocation ==="
    )

    assert stale_branch < idle_guard < archive < r2_preflight
    assert "is not provably idle; refusing to archive it or allocate GPU." in text


def test_phase8_transport_proof_is_bounded_by_first_dispatch_timeout() -> None:
    ltx_controlled = Path("scripts/pipeline/run_phase8_videos_controlled.ps1").read_text(
        encoding="utf-8"
    )
    ltx_runner = Path("scripts/pipeline/run_phase8_videos.py").read_text(encoding="utf-8")
    ltx_workflow = Path(
        "src/ai_video_factory/workflows/video_generation.py"
    ).read_text(encoding="utf-8")
    upscale_controlled = Path(
        "scripts/pipeline/run_phase8_upscale_controlled.ps1"
    ).read_text(encoding="utf-8")
    upscale_runner = Path("scripts/pipeline/run_phase8_upscale.py").read_text(encoding="utf-8")
    upscale_workflow = Path(
        "src/ai_video_factory/workflows/video_upscale.py"
    ).read_text(encoding="utf-8")

    for text in (ltx_controlled, upscale_controlled):
        assert "$DispatchTimeoutSeconds = 300" in text
        assert "--dispatch-timeout-seconds $DispatchTimeoutSeconds" in text

    for text in (ltx_runner, upscale_runner):
        assert '"--dispatch-timeout-seconds"' in text
        assert "dispatch_timeout_seconds=args.dispatch_timeout_seconds" in text

    for text in (ltx_workflow, upscale_workflow):
        assert "dispatch_timeout_seconds: float = 300.0" in text
        assert "transport_probe_ids" in text
        assert "dispatch_proven" in text
        assert "if transport_job_id is None" in text
        assert "queue.cancel(transport_job_id)" in text
