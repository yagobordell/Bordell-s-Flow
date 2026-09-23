from pathlib import Path

import pytest

from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.ideogram4 import IDEOGRAM4_MODEL_ID, Ideogram4Backend


def test_ideogram_missing_bootstrap_marker_is_explicitly_retryable(tmp_path: Path) -> None:
    backend = Ideogram4Backend(
        model_root=tmp_path / "ideogram4",
        model_repository=IDEOGRAM4_MODEL_ID,
        model_revision="main",
        bootstrap_status_path=tmp_path / "bootstrap.json",
    )

    with pytest.raises(ModelBootstrapPendingError, match="bootstrap marker is missing"):
        backend.prepare()
