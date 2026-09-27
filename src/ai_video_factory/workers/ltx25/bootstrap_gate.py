"""Gate LTX preparation on per-process verification of baked model provenance."""

from __future__ import annotations

import os
from pathlib import Path

from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.inference.tasks import TaskRunnerRegistry

_DEFAULT_MARKER = Path("/tmp/ai-video-factory/ltx25-baked-verified")
_TRUE = {"1", "true", "yes", "on"}


class LTXBakedBootstrapGate(TaskRunnerRegistry):
    """Keep /ready and Postgres polling closed until the runtime hash check ends."""

    @staticmethod
    def _require_verified_bootstrap() -> None:
        if os.environ.get("LTX_REQUIRE_BAKED_MODELS", "false").strip().lower() not in _TRUE:
            return

        session = os.environ.get("LTX_BOOTSTRAP_SESSION", "")
        marker = Path(os.environ.get("LTX_RUNTIME_BOOTSTRAP_MARKER") or _DEFAULT_MARKER)
        try:
            verified_session = marker.read_text(encoding="utf-8").strip()
        except OSError:
            verified_session = ""
        if not session or verified_session != session:
            raise ModelBootstrapPendingError(
                "LTX baked model checksum verification has not completed for this worker"
            )

    def prepare(self) -> None:
        self._require_verified_bootstrap()
        super().prepare()

    def ready(self) -> None:
        self._require_verified_bootstrap()
        super().ready()
