import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
PREWARM = Path("scripts/start_salad_optimized_prewarm.ps1")
PRODUCTION_RUNNER = Path("scripts/run_production.py")

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
    assert '"$InstancesUrl/$InstanceId/reallocate"' in text
    assert "current_queue_length" in text
    assert "@{ replicas = 1 }" in text
    assert "$Instances.Count -gt 1" in text
    assert "$Started -and" in text
    assert "$Ready" in text
    assert "prewarm complete: exactly one started ready replica, queue still empty" in text


def test_manifest_versions_and_download_profiles_are_explicit() -> None:
    services = json.loads(MANIFEST.read_text(encoding="utf-8"))["services"]

    assert services["ideogram4"]["image"].endswith("ideogram4-nf4-quality48-v3")
    assert services["breeze_tts2"]["image"].endswith("breeze-tts2-fast-all-v2")
    assert services["whisper"]["image"].endswith("whisper-large-v3-turbo-v2")
    assert services["ltx25"]["image"].endswith("ltx25-torch211-cu128-natten0216-xet-v3")

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


def test_controlled_gpu_runners_prewarm_and_always_stop() -> None:
    for path in ALL_CONTROLLED_RUNNERS:
        text = path.read_text(encoding="utf-8")
        assert "start_salad_optimized_prewarm.ps1" in text, path.name
        assert "finally" in text, path.name
        assert "-Action Stop" in text, path.name
        assert "-Action Status" in text, path.name


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
