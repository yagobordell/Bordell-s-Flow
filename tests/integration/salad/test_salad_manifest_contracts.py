import json
import re
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")


def _document() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_salad_manifest_centralizes_stack_identity_and_shared_environment() -> None:
    document = _document()

    assert document["schema_version"] == "2"
    assert document["stack"]["organization"] == "yagobordellorg"
    assert document["stack"]["project"] == "aivideofactory"
    assert document["stack"]["service_order"] == [
        "whisper",
        "breeze_tts2",
        "fish_speech",
        "ideogram4",
        "qwen_image_21",
        "ltx25",
        "realesrgan",
    ]
    assert document["stack"]["shared_required_environment"] == [
        "POSTGRES_DSN",
        "R2_ENDPOINT_URL",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ]
    assert document["stack"]["shared_environment"] == {
        "INFERENCE_WORKER_MODE": "production",
        "INFERENCE_WORKER_HEARTBEAT_SECONDS": "30",
        "SALAD_QUEUE_ENABLED": "true",
        "SALAD_LOG_LEVEL": "info",
    }
    assert document["stack"]["shared_liveness_probe"] == {
        "path": "/health",
        "period_seconds": 30,
        "timeout_seconds": 10,
        "failure_threshold": 20,
    }

    for service in document["services"].values():
        assert "organization" not in service
        assert "project" not in service
        assert "required_secrets" not in service
        assert "liveness" not in service["probes"]
        for name in document["stack"]["shared_environment"]:
            assert name not in service["environment"]
        assert service["autoscaler"]["min_replicas"] == 0


def test_every_model_has_its_own_group_and_queue() -> None:
    services = _document()["services"]
    groups = [service["group_name"] for service in services.values()]
    queues = [service["queue_name"] for service in services.values()]

    assert len(groups) == len(set(groups)) == 7
    assert len(queues) == len(set(queues)) == 7
    assert services["whisper"]["required_environment"] == ["HF_TOKEN"]
    assert "required_environment" not in services["breeze_tts2"]
    assert services["breeze_tts2"]["group_name"] == "ai-video-factory-breeze-tts2-worker-v2"
    assert services["fish_speech"]["required_environment"] == ["HF_TOKEN"]
    assert services["fish_speech"]["group_name"] == "ai-video-factory-fish-speech-worker"
    assert services["ideogram4"]["required_environment"] == ["HF_TOKEN"]
    assert services["qwen_image_21"]["required_environment"] == ["HF_TOKEN"]
    assert services["ltx25"]["required_environment"] == ["HF_TOKEN"]
    assert "required_environment" not in services["realesrgan"]


def test_worker_queue_display_names_match_salad_api_contract() -> None:
    display_name_pattern = re.compile(r"^[ A-Za-z0-9,.\-]{2,63}$")
    services = _document()["services"]

    for service in services.values():
        assert display_name_pattern.fullmatch(service["display_name"])
        assert display_name_pattern.fullmatch(f'{service["display_name"]} Jobs')

    script = WORKER_MANAGER.read_text(encoding="utf-8")
    assert 'display_name = "$($Definition.display_name) Jobs"' in script
    assert 'display_name = "$Service jobs"' not in script


def test_whisper_uses_fresh_versioned_group_after_queue_rebind_failure() -> None:
    services = _document()["services"]

    assert services["whisper"]["group_name"] == "ai-video-factory-whisper-worker-v5"
    assert services["whisper"]["queue_name"] == "ai-video-factory-whisper-jobs-v2"


def test_whisper_queue_rebind_uses_fresh_group_and_fresh_queue_pair() -> None:
    services = _document()["services"]
    whisper = services["whisper"]

    assert whisper["group_name"] == "ai-video-factory-whisper-worker-v5"
    assert whisper["queue_name"] == "ai-video-factory-whisper-jobs-v2"


def test_whisper_uses_manual_prewarm_lifecycle_consistently() -> None:
    document = _document()
    whisper = document["services"]["whisper"]
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert document["stack"]["autostart_policy"] is False
    assert "autostart_policy" not in whisper
    assert whisper["group_name"] == "ai-video-factory-whisper-worker-v5"
    assert whisper["queue_name"] == "ai-video-factory-whisper-jobs-v2"
    assert (
        '$ServiceAutostartProperty = $Definition.PSObject.Properties["autostart_policy"]'
        in script
    )
    assert "$AutostartPolicy = [bool]$ServiceAutostartProperty.Value" in script
