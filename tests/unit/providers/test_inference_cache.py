from pathlib import Path

import pytest

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.inference.storage import LocalObjectStorage, sha256_file
from ai_video_factory.providers.inference_jobs import cached_inference_response


def _request() -> InferenceJobRequest:
    job_id = "cache-sidecar-job"
    return InferenceJobRequest(
        job_id=job_id,
        task="test.sidecar",
        output=ObjectOutput(
            key=f"jobs/{job_id}/output.mp4",
            content_type="video/mp4",
        ),
        sidecar_outputs={
            "metadata": ObjectOutput(
                key=f"jobs/{job_id}/metadata.json",
                content_type="application/json",
            )
        },
    )


def _upload(
    storage: LocalObjectStorage,
    source: Path,
    *,
    key: str,
    content_type: str,
    metadata: dict[str, str],
) -> None:
    storage.upload(
        source,
        key,
        content_type=content_type,
        metadata=metadata,
    )


def test_cached_response_requires_every_declared_sidecar(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    request = _request()
    output = tmp_path / "output.mp4"
    output.write_bytes(b"video")
    output_sha = sha256_file(output)
    _upload(
        storage,
        output,
        key=request.output.key,
        content_type=request.output.content_type,
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": output_sha,
        },
    )

    assert cached_inference_response(storage, request) is None


def test_cached_response_accepts_complete_artifact_bundle(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    request = _request()
    output = tmp_path / "output.mp4"
    sidecar = tmp_path / "metadata.json"
    output.write_bytes(b"video")
    sidecar.write_text('{"ok":true}\n', encoding="utf-8")
    request_sha = request.fingerprint()

    _upload(
        storage,
        output,
        key=request.output.key,
        content_type=request.output.content_type,
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha,
            "artifact-sha256": sha256_file(output),
        },
    )
    _upload(
        storage,
        sidecar,
        key=request.sidecar_outputs["metadata"].key,
        content_type="application/json",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha,
            "artifact-sha256": sha256_file(sidecar),
            "sidecar-name": "metadata",
            "primary-artifact-sha256": sha256_file(output),
        },
    )

    response = cached_inference_response(storage, request)

    assert response is not None
    assert response.replayed is True
    assert response.output.key == request.output.key


def test_cached_response_rejects_foreign_sidecar_metadata(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    request = _request()
    output = tmp_path / "output.mp4"
    sidecar = tmp_path / "metadata.json"
    output.write_bytes(b"video")
    sidecar.write_text('{"ok":true}\n', encoding="utf-8")
    request_sha = request.fingerprint()

    _upload(
        storage,
        output,
        key=request.output.key,
        content_type=request.output.content_type,
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha,
            "artifact-sha256": sha256_file(output),
        },
    )
    _upload(
        storage,
        sidecar,
        key=request.sidecar_outputs["metadata"].key,
        content_type="application/json",
        metadata={
            "job-id": request.job_id,
            "request-sha256": "0" * 64,
            "artifact-sha256": sha256_file(sidecar),
            "sidecar-name": "metadata",
        },
    )

    with pytest.raises(RuntimeError, match="sidecar metadata"):
        cached_inference_response(storage, request)


def test_cached_response_rejects_sidecar_without_primary_binding(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")
    request = _request()
    output = tmp_path / "output-unbound.mp4"
    sidecar = tmp_path / "metadata-unbound.json"
    output.write_bytes(b"video")
    sidecar.write_text('{"ok":true}\n', encoding="utf-8")
    request_sha = request.fingerprint()

    _upload(
        storage,
        output,
        key=request.output.key,
        content_type=request.output.content_type,
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha,
            "artifact-sha256": sha256_file(output),
        },
    )
    _upload(
        storage,
        sidecar,
        key=request.sidecar_outputs["metadata"].key,
        content_type="application/json",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request_sha,
            "artifact-sha256": sha256_file(sidecar),
            "sidecar-name": "metadata",
        },
    )

    with pytest.raises(RuntimeError, match="sidecar metadata"):
        cached_inference_response(storage, request)
