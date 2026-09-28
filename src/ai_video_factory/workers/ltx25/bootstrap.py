from __future__ import annotations

import os
from pathlib import Path

from ai_video_factory.inference.errors import ModelBootstrapPendingError


def require_runtime_bootstrap_complete() -> None:
    """Gate readiness and job polling on the completed runtime bootstrap transaction."""

    configured = str(os.getenv("LTX_RUNTIME_BOOTSTRAP_MARKER") or "").strip()
    if not configured:
        return
    marker = Path(configured)
    if not marker.is_file():
        raise ModelBootstrapPendingError(
            f"LTX runtime bootstrap has not completed: missing marker {marker}"
        )
