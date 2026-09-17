from pathlib import Path
from typing import Any

from ai_video_factory.domain import VisualReference
from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.ideogram_rejections import safety_rejection_key
from ai_video_factory.providers.salad_flux import build_flux_job_request
from ai_video_factory.providers.salad_ideogram import (
    build_ideogram_job_request,
    reference_caption_variants,
)
from ai_video_factory.workers.flux_schnell import (
    FLUX_SCHNELL_MODEL_ID,
    FLUX_SCHNELL_REFERENCE_TASK,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)
from scripts.audit_phase4_reference_cache import audit_reference_cache


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=(
                "Canonical location reference. A distant rocky mountain range on a desert horizon."
            ),
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary",
                lighting="neutral daylight",
                medium="documentary photograph",
                render_mode="photo",
                render_description="realistic reference photography",
            ),
            background="Stable desert geography with broad passes and no temporary events.",
            elements=[],
        )
    )


class FakeStorage:
    def __init__(self, objects: dict[str, StoredObject]) -> None:
        self.objects = objects

    def stat(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def download(self, key: str, destination: Path) -> StoredObject:
        raise AssertionError("metadata-only planner must not download objects")

    def upload(self, *args: Any, **kwargs: Any) -> StoredObject:
        raise AssertionError("cache planner must never upload objects")

    def ping(self) -> None:
        return None


def _safety_rejection(request: Any, *, transport: str) -> StoredObject:
    return StoredObject(
        key=safety_rejection_key(request.job_id),
        content_type="application/json",
        size_bytes=100,
        etag="rejection-etag",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "rejection-kind": "ideogram_safety",
            "rejection-detail": "Ideogram 4 safety filter blocked generated image",
            "transport-job-id": transport,
        },
    )


def _cached_image(request: Any) -> StoredObject:
    return StoredObject(
        key=request.output.key,
        content_type="image/png",
        size_bytes=1234,
        etag="flux-etag",
        metadata={
            "job-id": request.job_id,
            "request-sha256": request.fingerprint(),
            "artifact-sha256": "b" * 64,
        },
    )


def _ideogram_requests() -> list[tuple[str, Any]]:
    return [
        (
            name,
            build_ideogram_job_request(
                task_name=IDEOGRAM4_REFERENCE_TASK,
                caption=caption,
                model_id=IDEOGRAM4_MODEL_ID,
                width=1024,
                height=1024,
            ),
        )
        for name, caption in reference_caption_variants(
            _caption(),
            task_name=IDEOGRAM4_REFERENCE_TASK,
        )
    ]


def test_phase4_audit_reports_terminal_safety_block_without_queue_work() -> None:
    requests = _ideogram_requests()
    executable = [(name, request) for name, request in requests if name != "safe_fallback"]
    objects = {
        safety_rejection_key(request.job_id): _safety_rejection(
            request,
            transport=f"transport-{index}",
        )
        for index, (_, request) in enumerate(executable, start=1)
    }
    storage = FakeStorage(objects)

    records = audit_reference_cache(
        [VisualReference(entity_id="location_004", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        flux_model_id=FLUX_SCHNELL_MODEL_ID,
        size="1024x1024",
    )

    record = records[0]
    assert record["status"] == "safety_blocked"
    assert record["primary_status"] == "safety_blocked"
    assert record["fallback_status"] == "miss"
    assert record["matched_provider"] is None
    assert set(record["safety_rejections"]) == {
        "canonical",
        "safe_simplified",
        "safe_minimal_art",
    }
    assert record["fallback_job_id"].startswith("flux-reference-")


def test_phase4_audit_uses_cached_flux_result_after_terminal_safety_block() -> None:
    requests = _ideogram_requests()
    executable = [(name, request) for name, request in requests if name != "safe_fallback"]
    objects = {
        safety_rejection_key(request.job_id): _safety_rejection(
            request,
            transport=f"transport-{index}",
        )
        for index, (_, request) in enumerate(executable, start=1)
    }
    flux_request = build_flux_job_request(
        task_name=FLUX_SCHNELL_REFERENCE_TASK,
        caption=_caption(),
        model_id=FLUX_SCHNELL_MODEL_ID,
        width=1024,
        height=1024,
    )
    objects[flux_request.output.key] = _cached_image(flux_request)
    storage = FakeStorage(objects)

    records = audit_reference_cache(
        [VisualReference(entity_id="location_004", prompt=_caption())],
        storage=storage,  # type: ignore[arg-type]
        model_id=IDEOGRAM4_MODEL_ID,
        flux_model_id=FLUX_SCHNELL_MODEL_ID,
        size="1024x1024",
    )

    record = records[0]
    assert record["status"] == "hit"
    assert record["primary_status"] == "safety_blocked"
    assert record["fallback_status"] == "hit"
    assert record["matched_provider"] == "flux_schnell"
    assert record["matched_variant"] == "safety_fallback"
    assert record["job_id"] == flux_request.job_id
