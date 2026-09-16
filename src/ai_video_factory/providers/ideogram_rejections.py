from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest
from ai_video_factory.inference.ports import ObjectStorage

_SAFETY_REJECTION_KIND = "ideogram_safety"
_SAFETY_DETAILS = frozenset(
    {
        "Ideogram 4 safety filter blocked generated image",
        "Ideogram 4 safety filter blocked all deterministic caption variants",
    }
)

# These application jobs were paid and confirmed safety-rejected before rejection caching
# existed. Keeping them here prevents an exact replay during migration to the R2 cache.
_HISTORICAL_SAFETY_REJECTIONS: dict[str, str | None] = {
    "ideogram-reference-6927a43b3213ae83dbf86a52c8722525": None,
    "ideogram-reference-e6a0d2f5296bab78dce70cfd30460fa6": (
        "51cd84a1f9c48ae3ed2f0f51f912a8e7cc9ad734cfabf942eec048b10c06c79d"
    ),
}


def is_safety_rejection_detail(detail: str) -> bool:
    return detail in _SAFETY_DETAILS


def safety_rejection_key(job_id: str) -> str:
    return f"jobs/{job_id}/rejections/safety.json"


def known_safety_rejection(storage: ObjectStorage, request: InferenceJobRequest) -> bool:
    historical_sha = _HISTORICAL_SAFETY_REJECTIONS.get(request.job_id, "missing")
    if historical_sha != "missing":
        return historical_sha is None or historical_sha == request.fingerprint()

    stored = storage.stat(safety_rejection_key(request.job_id))
    if stored is None:
        return False
    metadata = stored.metadata
    return (
        metadata.get("rejection-kind") == _SAFETY_REJECTION_KIND
        and metadata.get("job-id") == request.job_id
        and metadata.get("request-sha256") == request.fingerprint()
    )


def record_safety_rejection(
    storage: ObjectStorage,
    request: InferenceJobRequest,
    *,
    detail: str,
    transport_job_id: str | None,
) -> None:
    if not is_safety_rejection_detail(detail):
        raise ValueError("Only confirmed Ideogram safety rejections may be cached")

    payload = {
        "job_id": request.job_id,
        "request_sha256": request.fingerprint(),
        "rejection_kind": _SAFETY_REJECTION_KIND,
        "detail": detail,
        "transport_job_id": transport_job_id,
    }
    metadata = {
        "job-id": request.job_id,
        "request-sha256": request.fingerprint(),
        "rejection-kind": _SAFETY_REJECTION_KIND,
        "detail-sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
    }
    if transport_job_id:
        metadata["transport-job-id"] = transport_job_id

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "safety.json"
        source.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        storage.upload(
            source,
            safety_rejection_key(request.job_id),
            content_type="application/json",
            metadata=metadata,
        )
