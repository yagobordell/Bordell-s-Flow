from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ai_video_factory.workers.ltx25.a2v import DirectLTX25AudioToVideoBackend
from ai_video_factory.workers.ltx25.model import DirectLTX25Backend, LTXPipelineModeController


def test_both_ltx_modes_are_not_ready_before_successful_prepare(tmp_path: Path) -> None:
    controller = LTXPipelineModeController()
    video = DirectLTX25Backend(model_root=tmp_path, mode_controller=controller)
    audio_video = DirectLTX25AudioToVideoBackend(
        model_root=tmp_path, mode_controller=controller
    )
    with pytest.raises(RuntimeError, match="not been prepared"):
        video.ready()
    with pytest.raises(RuntimeError, match="not been prepared"):
        audio_video.ready()


def test_ltx_readiness_does_not_wait_for_generation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same mode lock is held for the complete >60-second real A2V job.
    # Readiness probes use a five-second timeout and must stay responsive.
    controller = LTXPipelineModeController()
    video = DirectLTX25Backend(model_root=tmp_path, mode_controller=controller)
    audio_video = DirectLTX25AudioToVideoBackend(
        model_root=tmp_path, mode_controller=controller
    )
    for backend in (video, audio_video):
        backend._prepared = True
        backend._bindings = object()
        monkeypatch.setattr(backend, "_validate_runtime", lambda bindings: None)

    errors: list[Exception] = []

    def check_both() -> None:
        try:
            video.ready()
            audio_video.ready()
        except Exception as exc:
            errors.append(exc)

    with controller.lock:
        probe = threading.Thread(target=check_both, daemon=True)
        probe.start()
        probe.join(timeout=2)
        assert not probe.is_alive(), "LTX /ready blocked on the inference mode lock"
    assert not errors
