import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_video_factory.inference.ports import StoredObject
from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.inference_jobs import RemoteInferenceRejectedError
from ai_video_factory.providers.salad_ideogram import (
    SaladIdeogramImageProvider,
    build_ideogram_job_request,
    reference_caption_variants,
)
from ai_video_factory.workers.ideogram4 import (
    IDEOGRAM4_GENERATION_PROFILE,
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_MODEL_ID,
    IDEOGRAM4_REFERENCE_TASK,
)


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description="A quiet mountain fortress at dawn.",
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary realism",
                lighting="soft dawn light",
                medium="documentary photograph",
                render_mode="photo",
                render_description="realistic 35mm photography",
            ),
            background="Timber walls, stone foundations, forested mountains.",
            elements=[],
        )
    )


def _road_caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description=(
                "Canonical location reference. A paved road crossing rocky terrain toward "
                "several white scientific dome buildings. Extra narrative sentence."
            ),
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary",
                lighting="even neutral reference lighting with clear readable form",
                medium="cinematic documentary reference photograph",
                render_mode="photo",
                render_description=(
                    "realistic reference photography, natural proportions, crisp material detail"
                ),
            ),
            background="Stable physical geography and architecture.",
            elements=[],
        )
    )


class EmptyStorage:
    def stat(self, key: str) -> None:
        del key
        return None


class FakeExecutor:
    def __init__(self, storage: Any | None = None) -> None:
        self.requests: list[Any] = []
        self.metadata: list[dict[str, str]] = []
        self.downloaded_response: Any = None
        self.storage = EmptyStorage() if storage is None else storage

    def _response(self, request: Any) -> Any:
        return SimpleNamespace(
            job_id=request.job_id,
            request_sha256=request.fingerprint(),
            replayed=False,
        )

    def execute(self, request: Any, *, metadata: dict[str, str]) -> Any:
        self.requests.append(request)
        self.metadata.append(metadata)
        return self._response(request)

    def download_output(self, response: Any, destination: Path) -> None:
        self.downloaded_response = response
        destination.write_bytes(b"\x89PNG\r\n\x1a\nideogram-output")


class SafetyThenSuccessExecutor(FakeExecutor):
    def execute(self, request: Any, *, metadata: dict[str, str]) -> Any:
        self.requests.append(request)
        self.metadata.append(metadata)
        if len(self.requests) == 1:
            raise RemoteInferenceRejectedError(
                request.job_id,
                "Ideogram 4 safety filter blocked all deterministic caption variants",
            )
        return self._response(request)


class ConcurrencyTrackingExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def execute(self, request: Any, *, metadata: dict[str, str]) -> Any:
        with self._lock:
            self.requests.append(request)
            self.metadata.append(metadata)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            return self._response(request)
        finally:
            with self._lock:
                self.active -= 1


class FallbackCacheStorage(EmptyStorage):
    def __init__(self, request: Any) -> None:
        self.request = request

    def stat(self, key: str) -> StoredObject | None:
        if key != self.request.output.key:
            return None
        return StoredObject(
            key=key,
            content_type="image/png",
            size_bytes=123,
            etag="etag",
            metadata={
                "job-id": self.request.job_id,
                "request-sha256": self.request.fingerprint(),
                "artifact-sha256": "a" * 64,
            },
        )


def test_salad_ideogram_provider_submits_reference_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )

    image = asyncio.run(
        provider.generate_image(
            prompt=_caption(),
            model=IDEOGRAM4_MODEL_ID,
            size="1024x1024",
            quality="high",
            output_format="png",
        )
    )

    request = executor.requests[0]
    assert request.task == IDEOGRAM4_REFERENCE_TASK
    assert request.inputs == []
    assert request.output.content_type == "image/png"
    assert request.parameters["generation_profile"] == IDEOGRAM4_GENERATION_PROFILE
    assert request.parameters["width"] == 1024
    assert request.parameters["height"] == 1024
    assert executor.metadata == [
        {
            "phase": "4",
            "provider": "ideogram4",
            "purpose": "reference",
            "prompt_variant": "canonical",
        }
    ]
    assert image.content.startswith(b"\x89PNG")
    assert image.metadata["prompt_variant"] == "canonical"
    assert image.metadata["job_id"] == request.job_id


def test_salad_ideogram_provider_submits_keyframe_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_KEYFRAME_TASK,
    )

    asyncio.run(
        provider.generate_image(
            prompt=_caption(),
            model=IDEOGRAM4_MODEL_ID,
            size="1024x1536",
            quality="auto",
            output_format="png",
        )
    )

    request = executor.requests[0]
    assert request.task == IDEOGRAM4_KEYFRAME_TASK
    assert request.parameters["width"] == 1024
    assert request.parameters["height"] == 1536
    assert executor.metadata == [
        {
            "phase": "6",
            "provider": "ideogram4",
            "purpose": "keyframe",
            "prompt_variant": "canonical",
        }
    ]


def test_salad_ideogram_provider_serializes_generation_by_default(tmp_path: Path) -> None:
    executor = ConcurrencyTrackingExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )

    async def generate_two() -> None:
        await asyncio.gather(
            provider.generate_image(
                prompt=_caption(),
                model=IDEOGRAM4_MODEL_ID,
                size="1024x1024",
                quality="high",
                output_format="png",
            ),
            provider.generate_image(
                prompt=_caption(),
                model=IDEOGRAM4_MODEL_ID,
                size="1024x1024",
                quality="high",
                output_format="png",
            ),
        )

    asyncio.run(generate_two())
    assert len(executor.requests) == 2
    assert executor.max_active == 1


def test_salad_ideogram_provider_rejects_invalid_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        SaladIdeogramImageProvider(
            executor=FakeExecutor(),  # type: ignore[arg-type]
            temp_dir=tmp_path,
            task_name=IDEOGRAM4_REFERENCE_TASK,
            max_concurrency=0,
        )


def test_salad_ideogram_provider_uses_structural_location_fallback_after_safety_block(
    tmp_path: Path,
) -> None:
    executor = SafetyThenSuccessExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )

    image = asyncio.run(
        provider.generate_image(
            prompt=_road_caption(),
            model=IDEOGRAM4_MODEL_ID,
            size="1024x1024",
            quality="high",
            output_format="png",
        )
    )

    assert len(executor.requests) == 2
    primary, recovery = executor.requests
    assert primary.job_id != recovery.job_id
    recovery_caption = json.loads(recovery.parameters["caption"])
    assert recovery_caption["high_level_description"] == (
        "Canonical location reference. A paved road crossing rocky terrain toward several white "
        "scientific dome buildings."
    )
    assert "Extra narrative sentence" not in recovery.parameters["caption"]
    assert executor.metadata[-1]["prompt_variant"] == "safe_fallback"
    assert image.metadata["prompt_variant"] == "safe_fallback"


def test_salad_ideogram_provider_reuses_cached_fallback_before_queue_submission(
    tmp_path: Path,
) -> None:
    variants = reference_caption_variants(
        _road_caption(),
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )
    fallback_request = build_ideogram_job_request(
        task_name=IDEOGRAM4_REFERENCE_TASK,
        caption=variants[1][1],
        model_id=IDEOGRAM4_MODEL_ID,
        width=1024,
        height=1024,
    )
    executor = FakeExecutor(storage=FallbackCacheStorage(fallback_request))
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )

    image = asyncio.run(
        provider.generate_image(
            prompt=_road_caption(),
            model=IDEOGRAM4_MODEL_ID,
            size="1024x1024",
            quality="high",
            output_format="png",
        )
    )

    assert executor.requests == []
    assert image.metadata["prompt_variant"] == "safe_fallback"
    assert image.metadata["replayed"] == "true"


def test_salad_ideogram_provider_rejects_plain_prompt_and_non_quality_mode(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_KEYFRAME_TASK,
    )

    with pytest.raises(ValueError, match="structured JSON"):
        asyncio.run(
            provider.generate_image(
                prompt="plain prompt",
                model=IDEOGRAM4_MODEL_ID,
                size="1024x1536",
                quality="high",
                output_format="png",
            )
        )

    with pytest.raises(ValueError, match="Quality mode"):
        asyncio.run(
            provider.generate_image(
                prompt=_caption(),
                model=IDEOGRAM4_MODEL_ID,
                size="1024x1536",
                quality="medium",
                output_format="png",
            )
        )

    assert executor.requests == []
