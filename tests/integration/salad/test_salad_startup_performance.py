import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
PREWARM = Path("scripts/salad/start_salad_optimized_prewarm.ps1")
QWEN_CONTROLLER = Path("scripts/pipeline/_qwen_controlled.ps1")

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
        assert "HF_XET_HIGH_PERFORMANCE" not in text, service
        if service == "whisper":
            assert "HF_HUB_DISABLE_XET=1" in text, service
        else:
            assert "HF_HUB_DISABLE_XET=1" not in text, service


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
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    services = document["services"]

    assert services["ideogram4"]["image"].endswith("ideogram4-nf4-quality48-v4")
    assert services["breeze_tts2"]["image"].endswith("breeze-tts2-fast-decode-v6")
    assert services["whisper"]["image"].endswith("whisper-large-v3-turbo-v5")
    assert document["stack"]["shared_environment"]["SALAD_LOG_LEVEL"] == "info"
    assert document["stack"]["autostart_policy"] is False
    assert "autostart_policy" not in services["whisper"]
    assert services["ltx25"]["image"].endswith("ltx25-a2v-torch211-cu128-eagersdpa-xet-lipsync-v6")
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


def test_phase6_uses_qwen_ready_hold_before_generation() -> None:
    phase6 = Path("scripts/pipeline/run_phase6_keyframes_controlled.ps1").read_text(
        encoding="utf-8"
    )
    controlled = QWEN_CONTROLLER.read_text(encoding="utf-8")
    prewarm = PREWARM.read_text(encoding="utf-8")
    qwen_profile = prewarm.split("    qwen_image_21 = @{", maxsplit=1)[1].split(
        "    }", maxsplit=1
    )[0]

    assert 'Phase = "Phase 6"' in phase6
    assert 'Service = "qwen_image_21"' in controlled
    assert "HoldReadyReplica = $true" in controlled
    assert "RunningNotReadySeconds = 1800" in qwen_profile
    assert "FinalRunningNotReadySeconds = 3000" in qwen_profile
    assert "[switch]$HoldReadyReplica" in prewarm
    assert "Test-RemoteAutoscalerBounds" in prewarm


def test_shared_ideogram_adoption_precedes_cold_state_requirement() -> None:
    text = PREWARM.read_text(encoding="utf-8")

    adopt = text.index("if (\n    $AdoptReadyReplica -and")
    cold_requirement = text.index(
        "Optimized prewarm requires '$GroupName' at replicas=0/pending=False in an allowed"
    )
    assert adopt < cold_requirement


def test_whisper_alignment_prewarm_budget_matches_cold_start_profile() -> None:
    controlled = Path("scripts/pipeline/run_phase5_alignment_controlled.ps1").read_text(
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
