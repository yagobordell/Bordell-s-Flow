"""Opt-in streaming guard tested without installed LTX, torch, CUDA or checkpoints."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from ai_video_factory.workers.ltx25 import a2v_streaming_guard as guard


class _FakeX0:
    def __init__(self, wrapper: object) -> None:
        self.velocity_model = wrapper
        self.training = True

    def eval(self) -> None:
        raise AssertionError("X0Model.eval() must not traverse the streaming wrapper")


class _FakeStream:
    def __init__(self, model: object) -> None:
        self._model = model
        self.training = True
        self.teardown_called = False


class _FakeStage:
    _is_streaming = True
    _device = "cpu"
    _dtype = "fake"
    _alloc_trim_strategy = "fake"

    def _prepared_builder(self) -> object:
        return object()


def _install_fake_upstream(
    monkeypatch: pytest.MonkeyPatch, stream: _FakeStream
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    @contextmanager
    def fake_streaming_model(*args: object):
        calls.append(args)
        try:
            yield stream
        finally:
            stream.teardown_called = True

    ltx_core = ModuleType("ltx_core")
    ltx_core.__path__ = []  # type: ignore[attr-defined]
    ltx_model = ModuleType("ltx_core.model")
    ltx_model.__path__ = []  # type: ignore[attr-defined]
    transformer = ModuleType("ltx_core.model.transformer")
    transformer.X0Model = _FakeX0  # type: ignore[attr-defined]
    ltx_pipelines = ModuleType("ltx_pipelines")
    ltx_pipelines.__path__ = []  # type: ignore[attr-defined]
    utils = ModuleType("ltx_pipelines.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    blocks = ModuleType("ltx_pipelines.utils.blocks")
    blocks._streaming_model = fake_streaming_model  # type: ignore[attr-defined]
    for module in (ltx_core, ltx_model, transformer, ltx_pipelines, utils, blocks):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls


def test_experimental_cpu_streaming_avoids_redundant_recursive_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = SimpleNamespace(training=False)
    stream = _FakeStream(raw)
    calls = _install_fake_upstream(monkeypatch, stream)
    first, second = _FakeStage(), _FakeStage()
    pipeline = SimpleNamespace(stage_1=first, stage_2=second)
    guard.configure_experimental_a2v_streaming(pipeline)

    for stage in (first, second):
        with stage._streaming_transformer_ctx() as model:
            assert model.training is False
            assert stream.training is False
            assert raw.training is False
            assert model.velocity_model is stream
        assert stream.teardown_called
    assert len(calls) == 2


def test_experimental_guard_rejects_partial_patch() -> None:
    first = _FakeStage()
    second = SimpleNamespace(_is_streaming=False)
    pipeline = SimpleNamespace(stage_1=first, stage_2=second)
    with pytest.raises(RuntimeError, match="refusing partial patch"):
        guard.configure_experimental_a2v_streaming(pipeline)
    assert "_streaming_transformer_ctx" not in vars(first)


def test_experimental_guard_rejects_non_eval_meta_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeStream(SimpleNamespace(training=True))
    _install_fake_upstream(monkeypatch, stream)
    pipeline = SimpleNamespace(stage_1=_FakeStage(), stage_2=_FakeStage())
    guard.configure_experimental_a2v_streaming(pipeline)
    with pytest.raises(RuntimeError, match="must already be in eval mode"):
        with pipeline.stage_1._streaming_transformer_ctx():
            pass
    assert stream.teardown_called
