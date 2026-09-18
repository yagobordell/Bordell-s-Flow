from pathlib import Path


RUNNER = Path("scripts/run_phase8_videos_controlled.ps1")


def test_phase8_resume_starts_scale_to_zero_group_for_existing_jobs() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert 'video_generation_manifest.json' in text
    assert '$ResumeSubmittedJobs = $SubmittedJobs.Count -gt 0' in text
    assert 'manage_salad_worker.ps1' in text
    assert 'Action = "Start"' in text
    assert 'Service = "ltx25"' in text
    assert 'if ($ResumeSubmittedJobs)' in text
    assert 'start_salad_optimized_prewarm.ps1' in text


def test_phase8_resume_detection_happens_before_gpu_allocation() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert text.index('$ManifestPath = Join-Path $OutputDir') < text.index(
        '=== R2 preflight: verify storage before GPU allocation ==='
    )
    assert text.index('if ($ResumeSubmittedJobs)') < text.index(
        '=== Phase 8 video generation: worker group available; resume/fanout active ==='
    )
