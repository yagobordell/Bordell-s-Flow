"""Baked weights may not enter LTX preparation before runtime SHA validation."""

from __future__ import annotations

import pytest

from ai_video_factory.inference.errors import ModelBootstrapPendingError
from ai_video_factory.workers.ltx25.bootstrap_gate import LTXBakedBootstrapGate


class _Runner:
    task_name = "ltx.test"

    def __init__(self) -> None:
        self.prepare_calls = 0
        self.ready_calls = 0

    def prepare(self) -> None:
        self.prepare_calls += 1

    def ready(self) -> None:
        self.ready_calls += 1


def test_baked_gate_requires_matching_fresh_process_session(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "verified"
    monkeypatch.setenv("LTX_REQUIRE_BAKED_MODELS", "true")
    monkeypatch.setenv("LTX_BOOTSTRAP_SESSION", "current-session")
    monkeypatch.setenv("LTX_RUNTIME_BOOTSTRAP_MARKER", str(marker))
    runner = _Runner()
    registry = LTXBakedBootstrapGate([runner])

    with pytest.raises(ModelBootstrapPendingError):
        registry.prepare()
    marker.write_text("previous-session\n", encoding="utf-8")
    with pytest.raises(ModelBootstrapPendingError):
        registry.ready()
    assert runner.prepare_calls == runner.ready_calls == 0

    marker.write_text("current-session\n", encoding="utf-8")
    registry.prepare()
    registry.ready()
    assert runner.prepare_calls == runner.ready_calls == 1
