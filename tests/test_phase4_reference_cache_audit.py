from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.salad_ideogram import build_ideogram_job_request
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)
from scripts.audit_phase4_reference_cache import audit_reference_cache


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description="Canonical location reference. A desert observatory.",
            style=IdeogramStylePlan(
                aesthetics="documentary realism",
                lighting="neutral daylight",
                medium="documentary photograph",
                render_mode="photo",
                render_description="realistic reference photography",
            ),
            background="Clear desert geography around the observatory.",
            elements=[],
        )
    )


class FakeStorage:
    def __init__(self, objects: dict[str, StoredObject]) -> None:
        self.objects = objects
        self.downloads = 0

    def stat(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def download(self, key: str, destination: Path) -> StoredObject:
        self.downloads += 1
        raise AssertionError("metadata-only audit must not download objects")

    def upload(self, *args: Any, **kwargs: Any) -> StoredObject:
        raise AssertionError("cache audit must never upload objects")

    def ping(self) -> None:
        return None


def _request():
    return build_ideogram_job_request(
        task_name=IDEOGRAM4_REFERENCE_TASK,
        caption=_caption(),
        model_id=IDEOGRAM4_MODEL_ID,
        width=1024,
        height=1024,
    )


def test_phase4_cache_audit_reports_verified_hit_without_download() -> None:
    request = _request()
    artifact_sha256 = "a" * 64
    storage = FakeStorage(
        {
            request.output.key: StoredObject(
                key=request.output.key,
                content_type="image/png",
                size_bytes=321,
                etag="etag-1",
                metadata={
                    "job-id": request.job_id,
                    "request-sha256": request.fingerprint(),
                    "artifact-sha256": artifact_sha256,
                },
            )
        }
    )

    records = audit_reference_cache(
        [VisualReference(entity_id="location_001", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    assert records == [
        {
            "entity_id": "location_001",
            "status": "hit",
            "job_id": request.job_id,
            "object_key": request.output.key,
            "expected_request_sha256": request.fingerprint(),
            "expected_content_type": "image/png",
            "stored_job_id": request.job_id,
            "stored_request_sha256": request.fingerprint(),
            "stored_artifact_sha256": artifact_sha256,
            "stored_content_type": "image/png",
            "stored_size_bytes": 321,
            "stored_etag": "etag-1",
            "content_sha256_verified": None,
            "error": None,
        }
    ]
    assert storage.downloads == 0


def test_phase4_cache_audit_reports_invalid_metadata_without_mutating_storage() -> None:
    request = _request()
    storage = FakeStorage(
        {
            request.output.key: StoredObject(
                key=request.output.key,
                content_type="image/png",
                size_bytes=321,
                etag="etag-1",
                metadata={
                    "job-id": request.job_id,
                    "request-sha256": "wrong-fingerprint",
                    "artifact-sha256": "a" * 64,
                },
            )
        }
    )

    records = audit_reference_cache(
        [VisualReference(entity_id="location_001", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        size="1024x1024",
    )

    assert records[0]["status"] == "invalid"
    assert "metadata does not match" in records[0]["error"]
    assert storage.downloads == 0


def test_phase4_cache_audit_script_has_no_salad_queue_client() -> None:
    script = Path("scripts/audit_phase4_reference_cache.py").read_text(encoding="utf-8")

    assert "SaladJobQueueClient" not in script
    assert "providers.salad_queue" not in script
    assert "queue.submit" not in script
    assert "Salad queue submissions=0" in script
