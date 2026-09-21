import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")
PRODUCTION_RUNNER = Path("scripts/run_production.py")
R2_PREFLIGHT = Path("scripts/check_r2_ready.py")
QUEUE_CLEANUP = Path("scripts/cleanup_salad_queue.ps1")
CONFIG = Path("src/ai_video_factory/config.py")

WORKERS = {
    "ideogram4": Path("docker/workers/ideogram4/download_models.sh"),
    "breeze_tts2": Path("docker/workers/breeze-tts2/download_models.sh"),
    "whisper": Path("docker/workers/whisper/download_models.sh"),
}

DOCKERFILES = {
    "ideogram4": Path("docker/workers/ideogram4/Dockerfile"),
    "breeze_tts2": Path("docker/workers/breeze-tts2/Dockerfile"),
    "whisper": Path("docker/workers/whisper/Dockerfile"),
}

READY_BEFORE_QUEUE_RUNNERS = (
    Path("scripts/run_phase4_assets_controlled.ps1"),
    Path("scripts/run_phase5_audio_controlled.ps1"),
    Path("scripts/run_phase5_alignment_controlled.ps1"),
    Path("scripts/run_phase6_keyframes_controlled.ps1"),
)

ALL_CONTROLLED_RUNNERS = READY_BEFORE_QUEUE_RUNNERS + (
    Path("scripts/run_phase8_videos_controlled.ps1"),
)

GPU_CLIENTS = (
    Path("scripts/run_phase4_assets.py"),
    Path("scripts/run_phase5_audio.py"),
    Path("scripts/run_phase5_alignment.py"),
    Path("scripts/run_phase6_keyframes.py"),
    Path("scripts/run_phase8_videos.py"),
)


def test_all_hf_workers_reject_slow_model_download_nodes() -> None:
    for service, path in WORKERS.items():
        text = path.read_text(encoding="utf-8")
        assert "ai_video_factory.workers.download_watchdog" in text, service
        assert "--min-throughput-mibps" in text, service
        assert "--reallocate-on-slow" in text, service
        assert "HF_HUB_DOWNLOAD_TIMEOUT" in text, service
        assert "HF_HUB_ETAG_TIMEOUT" in text, service


def test_xet_is_enabled_without_unsafe_high_performance_mode() -> None:
    for service, path in DOCKERFILES.items():
        text = path.read_text(encoding="utf-8")
        assert "hf-xet==1.6.0" in text, service
        assert "HF_HUB_DISABLE_XET=1" not in text, service
        assert "HF_XET_HIGH_PERFORMANCE" not in text, service


def test_optimized_prewarm_applies_bounded_node_selection_to_every_worker() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    for service in ("whisper", "breeze_tts2", "ideogram4", "ltx25"):
        assert f"    {service} = @{{" in text
    assert "MaxAllocatingReallocations" in text
    assert "MaxImagePullReallocations" in text
    assert "MaxRunningNotReadyReallocations" in text
    assert "MaxNodeChanges" in text
    assert "$NodeChanges" in text
    assert "exceeded the global node-change budget" in text
    assert '"$InstancesUrl/$InstanceId/reallocate"' in text
    assert "current_queue_length" in text
    assert "@{ replicas = 1 }" in text
    assert "$Instances.Count -gt 1" in text
    assert "$ContainerStarted = $Started -or $ContainerObservedRunning" in text
    assert "$ContainerStarted -and" in text
    assert "$Ready" in text
    assert "prewarm complete: exactly one started ready replica" in text
    assert "first real job will prove transport" in text


def test_ideogram_prewarm_has_specific_finite_runtime_and_node_budget() -> None:
    text = PREWARM.read_text(encoding="utf-8")
    ideogram_profile = text.split("    ideogram4 = @{", maxsplit=1)[1].split("    }", maxsplit=1)[0]

    assert "RunningNotReadySeconds = 900" in ideogram_profile
    assert "FinalRunningNotReadySeconds = 900" in ideogram_profile
    assert "MaxRunningNotReadyReallocations = 1" in ideogram_profile
    assert "MaxNodeChanges = 2" in ideogram_profile
    assert 'max_replicas -ne 1' in text


def test_manifest_versions_and_download_profiles_are_explicit() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]

    assert services["ideogram4"]["image"].endswith("ideogram4-nf4-quality48-v4")
    assert services["breeze_tts2"]["image"].endswith("breeze-tts2-fast-all-v3")
    assert services["whisper"]["image"].endswith("whisper-large-v3-turbo-v4")
    assert services["ideogram4"]["environment"]["SALAD_LOG_LEVEL"] == "info"
    assert services["whisper"]["environment"]["SALAD_LOG_LEVEL"] == "info"
    assert services["whisper"]["autostart_policy"] is False
    assert services["ltx25"]["image"].endswith("ltx25-torch211-cu128-eagersdpa-xet-v4")
    assert services["ltx25"]["environment"]["PYTHONFAULTHANDLER"] == "1"

    assert services["ideogram4"]["autoscaler"]["max_replicas"] == 1
    for service in services.values():
        assert service["autoscaler"]["min_replicas"] == 0

    for service, prefix in (
        ("ideogram4", "IDEOGRAM"),
        ("breeze_tts2", "BREEZE"),
        ("whisper", "WHISPER"),
    ):
        environment = services[service]["environment"]
        assert environment[f"{prefix}_DOWNLOAD_MIN_MIBPS"]
        assert environment[f"{prefix}_DOWNLOAD_STALL_TIMEOUT_SECONDS"]
        assert environment[f"{prefix}_DOWNLOAD_HARD_TIMEOUT_SECONDS"]

    ideogram = services["ideogram4"]["environment"]
    assert ideogram["IDEOGRAM_BOOTSTRAP_STALL_TIMEOUT_SECONDS"] == "720"
    assert ideogram["IDEOGRAM_BOOTSTRAP_HARD_TIMEOUT_SECONDS"] == "900"
    assert ideogram["IDEOGRAM_BOOTSTRAP_REALLOCATE_ON_STALL"] == "true"


def test_controlled_gpu_runners_prewarm_and_always_stop() -> None:
    for path in ALL_CONTROLLED_RUNNERS:
        text = path.read_text(encoding="utf-8")
        assert "start_salad_optimized_prewarm.ps1" in text, path.name
        assert "finally" in text, path.name
        assert "-Action Stop" in text, path.name
        assert "-Action Status" in text, path.name


def test_ideogram_controlled_runners_pin_warm_and_clean_queue() -> None:
    assert QUEUE_CLEANUP.is_file()
    cleanup = QUEUE_CLEANUP.read_text(encoding="utf-8")
    assert "foreach ($Job in $ActiveJobs)" in cleanup
    assert "Cancelling abandoned active job after group stop" in cleanup
    assert "Waiting for cancelled running job(s)" in cleanup
    assert "requires '$GroupName' fully stopped first" in cleanup

    for path in (
        Path("scripts/run_phase4_assets_controlled.ps1"),
        Path("scripts/run_phase6_keyframes_controlled.ps1"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "HoldReadyReplica = $true" in text, path.name
        assert "hold_salad_warm_replica.ps1" not in text, path.name
        assert "cleanup_salad_queue.ps1" in text, path.name
        assert text.index("-Action Stop") < text.index("& $QueueCleanup"), path.name
        assert text.index("& $QueueCleanup") < text.index("-Action Status"), path.name


def test_controlled_gpu_runners_preflight_r2_before_prewarm() -> None:
    assert R2_PREFLIGHT.is_file()
    for path in ALL_CONTROLLED_RUNNERS:
        text = path.read_text(encoding="utf-8")
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
        assert "from r2_client import create_r2_storage" in text, path.name
        assert "R2ObjectStorage.create(" not in text, path.name


def test_ready_before_queue_runners_keep_short_pending_deadline() -> None:
    for path in READY_BEFORE_QUEUE_RUNNERS:
        text = path.read_text(encoding="utf-8")
        assert "pending-timeout-seconds" in text, path.name


def test_phase5_clients_have_separate_pending_deadline() -> None:
    for path in (
        Path("scripts/run_phase5_audio.py"),
        Path("scripts/run_phase5_alignment.py"),
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



def test_phase8_resume_starts_scale_to_zero_group_only_for_active_current_jobs() -> None:
    text = Path("scripts/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

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
    text = Path("scripts/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert "deploy\\salad\\services.json" in text
    assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
    assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
    assert "$env:SALAD_LTX25_QUEUE_NAME = [string]$LtxService.queue_name" in text
    assert "Phase 8 canonical Salad route" in text


def test_phase8_resume_detection_happens_before_gpu_allocation() -> None:
    text = Path("scripts/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

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
    text = Path("scripts/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

    assert "check_salad_queue_ready.py" in text
    assert "& python $QueueGuard ltx25 --output-dir $ProductionOutputRoot" in text
    assert "LTX resume queue ownership guard failed; refusing GPU allocation." in text
    assert text.index(
        "& python $QueueGuard ltx25 --output-dir $ProductionOutputRoot"
    ) < text.index("& $ScaleToZeroStarter @StartArguments")


def test_phase8_archives_stale_manifest_only_after_idle_queue_guard() -> None:
    text = Path("scripts/run_phase8_videos_controlled.ps1").read_text(encoding="utf-8")

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



def test_phase6_reuses_shared_ideogram_replica_before_cold_prewarm() -> None:
    phase6 = Path("scripts/run_phase6_keyframes_controlled.ps1").read_text(
        encoding="utf-8"
    )
    prewarm = PREWARM.read_text(encoding="utf-8")

    assert '$PrewarmArguments["AdoptReadyReplica"] = $true' in phase6
    assert "if ($ReleaseSharedIdeogram)" in phase6
    assert "[switch]$AdoptReadyReplica" in prewarm
    assert "$AdoptReadyReplica -and" in prewarm
    assert '$Status -eq "running"' in prewarm
    assert "[int]$Group.replicas -eq 1" in prewarm
    assert "[int]$HeldAutoscaler.min_replicas -ne 1" in prewarm
    assert "Test-RemoteAutoscalerMatchesManifestExceptMinReplicas" in prewarm
    assert "$HeldInstances.Count -ne 1" in prewarm
    assert "$HeldStarted" in prewarm
    assert "$HeldReady" in prewarm
    assert "Assert-QueueLogicallyEmpty -Queue $Queue -VerificationSeconds 180" in prewarm
    assert "prewarm adopted one already started+ready shared replica" in prewarm


def test_shared_ideogram_adoption_precedes_cold_state_requirement() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    adopt = text.index("if (\n    $AdoptReadyReplica -and")
    cold_requirement = text.index(
        "Optimized prewarm requires '$GroupName' at replicas=0/pending=False in an allowed"
    )
    assert adopt < cold_requirement



def test_phase6_recovers_once_from_hung_ideogram_inference() -> None:
    text = Path("scripts/run_phase6_keyframes_controlled.ps1").read_text(
        encoding="utf-8"
    )
    runner = Path("scripts/run_phase6_keyframes.py").read_text(encoding="utf-8")

    assert "[int]$RunningTimeoutSeconds = 1200" in text
    assert "[int]$IdeogramRecoveryRetries = 1" in text
    assert "$MaxPhase6Attempts = 1 + $IdeogramRecoveryRetries" in text
    assert "$Phase6ExitCode -eq 75" in text
    assert "Recycling the worker" in text
    assert text.index("-Action Stop -Service ideogram4") < text.index(
        "=== Ideogram recovery prewarm: start one fresh ready replica ==="
    )
    assert text.index("& $QueueCleanup -Service ideogram4") < text.index(
        "=== Ideogram recovery prewarm: start one fresh ready replica ==="
    )
    assert "PHASE6_IDEOGRAM_RUNNING_TIMEOUT" in runner
    assert 'exc.job_id.startswith("ideogram-keyframe-")' in runner
    assert "IDEOGRAM_RUNNING_TIMEOUT_EXIT_CODE = 75" in runner


def test_phase6_emits_inference_progress_logs() -> None:
    runner = Path("scripts/run_phase6_keyframes.py").read_text(encoding="utf-8")
    executor = Path(
        "src/ai_video_factory/providers/inference_jobs.py"
    ).read_text(encoding="utf-8")

    assert "logging.basicConfig(" in runner
    assert "Inference transport submitted" in executor
    assert "Inference transport progress" in executor
    assert "elapsed_seconds=%.1f" in executor



def test_whisper_alignment_prewarm_budget_matches_cold_start_profile() -> None:
    controlled = Path("scripts/run_phase5_alignment_controlled.ps1").read_text(
        encoding="utf-8"
    )
    prewarm = PREWARM.read_text(encoding="utf-8")
    whisper_profile = prewarm.split("    whisper = @{", maxsplit=1)[1].split(
        "    }", maxsplit=1
    )[0]

    assert "[int]$PrewarmTimeoutMinutes = 120" in controlled
    assert "TimeoutMinutes = $PrewarmTimeoutMinutes" in controlled
    assert "RunningNotReadySeconds = 1800" in whisper_profile
    assert "FinalRunningNotReadySeconds = 3000" in whisper_profile
    assert "MaxRunningNotReadyReallocations = 1" in whisper_profile



def test_phase5_alignment_pins_canonical_salad_route() -> None:
    text = Path("scripts/run_phase5_alignment_controlled.ps1").read_text(
        encoding="utf-8"
    )

    assert "deploy\\salad\\services.json" in text
    assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
    assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
    assert "$env:SALAD_WHISPER_QUEUE_NAME = [string]$WhisperService.queue_name" in text
    assert '"--queue-name", $env:SALAD_WHISPER_QUEUE_NAME' in text
    assert "Phase 5 canonical Salad route" in text



def test_whisper_default_queue_matches_manifest() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]
    config = CONFIG.read_text(encoding="utf-8")

    expected = services["whisper"]["queue_name"]
    assert f'salad_whisper_queue_name: str = "{expected}"' in config



def test_ideogram_controlled_runners_pin_canonical_salad_routes() -> None:
    for path in (
        Path("scripts/run_phase4_assets_controlled.ps1"),
        Path("scripts/run_phase6_keyframes_controlled.ps1"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "deploy\\salad\\services.json" in text, path.name
        assert "$env:SALAD_ORGANIZATION = [string]$Services.stack.organization" in text
        assert "$env:SALAD_PROJECT = [string]$Services.stack.project" in text
        assert (
            "$env:SALAD_IDEOGRAM4_QUEUE_NAME = [string]$IdeogramService.queue_name"
            in text
        )
        assert (
            "$env:SALAD_FLUX2_KLEIN_QUEUE_NAME = [string]$FluxService.queue_name"
            in text
        )
        assert '"--queue-name", $env:SALAD_IDEOGRAM4_QUEUE_NAME' in text
        assert '"--fallback-queue-name", $env:SALAD_FLUX2_KLEIN_QUEUE_NAME' in text
        assert "canonical Salad routes: ideogram={0} flux={1}" in text



def test_image_queue_defaults_match_manifest() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]
    config = CONFIG.read_text(encoding="utf-8")

    ideogram = services["ideogram4"]["queue_name"]
    flux = services["flux2_klein"]["queue_name"]
    assert f'salad_ideogram4_queue_name: str = "{ideogram}"' in config
    assert f'salad_flux2_klein_queue_name: str = "{flux}"' in config



def test_phase8_transport_proof_is_bounded_by_first_dispatch_timeout() -> None:
    ltx_controlled = Path("scripts/run_phase8_videos_controlled.ps1").read_text(
        encoding="utf-8"
    )
    ltx_runner = Path("scripts/run_phase8_videos.py").read_text(encoding="utf-8")
    ltx_workflow = Path(
        "src/ai_video_factory/workflows/video_generation.py"
    ).read_text(encoding="utf-8")
    upscale_controlled = Path(
        "scripts/run_phase8_upscale_controlled.ps1"
    ).read_text(encoding="utf-8")
    upscale_runner = Path("scripts/run_phase8_upscale.py").read_text(encoding="utf-8")
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
        assert "queue.cancel(state.transport_job_id)" in text
