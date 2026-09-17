import asyncio
from pathlib import Path
from typing import Any

import pytest

from ai_video_factory.providers.ideogram_caption import (
    IdeogramCaptionPlan,
    IdeogramStylePlan,
    render_ideogram_caption,
)
from ai_video_factory.providers.inference_jobs import RemoteInferenceRejectedError
from ai_video_factory.providers.salad_ideogram import SaladIdeogramImageProvider
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_MODEL_ID, IDEOGRAM4_REFERENCE_TASK


class FailingSafetyCacheStorage:
    def stat(self, key: str):
        del key
        return None

    def upload(
        self,
        source: Path,
        key: str,
        *,
        content_type: str,
        metadata: dict[str, str],
    ):
        del source, key, content_type, metadata
        raise TimeoutError("transient R2 write timeout")


class AlwaysSafetyExecutor:
    def __init__(self) -> None:
        self.storage = FailingSafetyCacheStorage()
        self.requests: list[Any] = []

    def execute(self, request: Any, *, metadata: dict[str, str]):
        del metadata
        self.requests.append(request)
        raise RemoteInferenceRejectedError(
            request.job_id,
            "Ideogram 4 safety filter blocked generated image",
            transport_job_id=f"transport-{len(self.requests)}",
        )

    def download_output(self, response: Any, destination: Path) -> None:
        raise AssertionError(f"unexpected output download: {response} -> {destination}")


def _caption() -> str:
    return render_ideogram_caption(
        IdeogramCaptionPlan(
            high_level_description="Canonical location reference. A small greenhouse at dawn.",
            style=IdeogramStylePlan(
                aesthetics="cinematic documentary",
                lighting="soft dawn light",
                medium="reference photograph",
                render_mode="photo",
                render_description="realistic reference photography",
            ),
            background="Glass walls beside a rain-filled path.",
            elements=[],
        )
    )


def test_transient_safety_cache_write_failure_preserves_terminal_provider_rejection(
    tmp_path: Path,
) -> None:
    executor = AlwaysSafetyExecutor()
    provider = SaladIdeogramImageProvider(
        executor=executor,  # type: ignore[arg-type]
        temp_dir=tmp_path,
        task_name=IDEOGRAM4_REFERENCE_TASK,
    )

    with pytest.raises(RemoteInferenceRejectedError, match="all provider caption variants"):
        asyncio.run(
            provider.generate_image(
                prompt=_caption(),
                model=IDEOGRAM4_MODEL_ID,
                size="1024x1024",
                quality="high",
                output_format="png",
            )
        )

    assert len(executor.requests) == 3


def test_phase4_wrapper_cleans_flux_when_safety_is_discovered_during_ideogram_work() -> None:
    script = Path("scripts/run_phase4_assets_controlled.ps1").read_text(encoding="utf-8")

    assert "$FluxCleanupRequired = $IdeogramNeeded -or $FluxNeeded" in script
    assert "if ($FluxCleanupRequired)" in script
