import json
from pathlib import Path

MANIFEST = Path("deploy/salad/services.json")
WORKER_MANAGER = Path("scripts/salad/manage_salad_worker.ps1")
ENTRYPOINT = Path("docker/workers/common/entrypoint.sh")
REPOSITORY = Path("src/ai_video_factory/inference/repository.py")
AUTOSCALER = Path("src/ai_video_factory/inference/salad_autoscaler.py")
COORDINATION = Path("src/ai_video_factory/inference/coordination.py")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_salad_manifest_uses_postgres_as_the_only_job_transport() -> None:
    document = _manifest()

    assert document["schema_version"] == 3
    assert document["stack"]["job_transport"] == "postgres"
    assert document["stack"]["shared_environment"]["INFERENCE_WORKER_POLL_JOBS"] == "true"
    assert document["stack"]["shared_environment"]["INFERENCE_WORKER_JOB_POLL_SECONDS"] == "2"
    assert "SALAD_QUEUE_ENABLED" not in document["stack"]["shared_environment"]
    assert "SALAD_LOG_LEVEL" not in document["stack"]["shared_environment"]

    groups: list[str] = []
    for service in document["services"].values():
        groups.append(service["group_name"])
        assert "queue_name" not in service
        assert "autoscaler" not in service
        assert service["capacity"]["start_replicas"] >= 1
        assert service["capacity"]["max_replicas"] >= service["capacity"]["start_replicas"]

    assert len(groups) == len(set(groups))


def test_worker_manager_is_compute_only_and_migrates_legacy_groups() -> None:
    script = WORKER_MANAGER.read_text(encoding="utf-8")

    assert '[ValidateRange(0, 64)][int]$Replicas = 0' in script
    assert '$Definition.capacity.start_replicas' in script
    assert '$Definition.capacity.max_replicas' in script
    assert "function Test-LegacyQueueAttachment" in script
    assert '@("queue_connection", "queue_autoscaler")' in script
    assert "Recreating '$GroupName' to remove Salad Job Queue/autoscaler state." in script
    assert "queue_connection =" not in script
    assert "queue_autoscaler =" not in script
    assert "Ensure-Queue" not in script
    assert "$QueuesBase" not in script
    assert "set explicit replica capacity" in script
    assert "Wait-ForStoppedGroup" in script
    assert "stable stopped/pending_change=false" in script
    assert "set replicas to zero" not in script


def test_worker_entrypoint_has_no_salad_queue_sidecar() -> None:
    script = ENTRYPOINT.read_text(encoding="utf-8")

    assert "salad-http-job-queue-worker" not in script
    assert "SALAD_QUEUE_ENABLED" not in script
    assert "polling canonical Postgres jobs" in script
    assert "wait_for_endpoint /health" in script
    assert "wait_for_endpoint /ready" in script


def test_drain_publication_and_claim_share_instance_lock() -> None:
    repository = REPOSITORY.read_text(encoding="utf-8")
    autoscaler = AUTOSCALER.read_text(encoding="utf-8")
    coordination = COORDINATION.read_text(encoding="utf-8")

    assert "pg_advisory_xact_lock" in coordination
    assert "acquire_instance_drain_lock(connection, instance_id)" in repository
    assert "acquire_instance_drain_lock(connection, instance_id)" in autoscaler
    assert "FROM gpu.capacity_drains" in repository
