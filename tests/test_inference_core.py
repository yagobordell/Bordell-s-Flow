from ai_video_factory.inference.contracts import (
    InferenceJobRequest,
    InferenceJobResponse,
    ObjectOutput,
)
from ai_video_factory.inference.settings import InferenceWorkerSettings


def test_inference_job_can_start_without_object_inputs() -> None:
    request = InferenceJobRequest(
        job_id="ideogram-reference-001",
        task="image.reference.generate",
        output=ObjectOutput(
            key="jobs/ideogram-reference-001/reference.png",
            content_type="image/png",
        ),
        parameters={"prompt": "A studio portrait"},
    )

    assert request.inputs == []
    assert len(request.fingerprint()) == 64


def test_local_inference_settings_do_not_require_cloud_secrets() -> None:
    settings = InferenceWorkerSettings(
        _env_file=None,
        worker_mode="local",
        worker_lease_seconds=60,
        worker_heartbeat_seconds=10,
    )

    assert settings.worker_mode == "local"
    assert settings.local_object_root.as_posix() == "data/inference-local"


def test_optional_sidecars_do_not_change_legacy_request_serialization() -> None:
    request = InferenceJobRequest(
        job_id="legacy-no-sidecar",
        task="infrastructure.copy",
        output=ObjectOutput(
            key="jobs/legacy-no-sidecar/output.txt",
            content_type="text/plain",
        ),
    )

    document = request.model_dump(mode="json", exclude_none=True)

    assert "sidecar_outputs" not in document
    assert "max_attempts" not in document
    assert len(request.fingerprint()) == 64
