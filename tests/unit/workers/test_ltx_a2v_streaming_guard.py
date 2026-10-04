"""Experimental-only A2V streaming workaround for the redundant recursive eval.

These tests use a small torch model, no CUDA or downloaded checkpoints.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import torch

import ai_video_factory.workers.ltx25.reference_a2v as reference_a2v
import ltx_pipelines.utils.blocks as upstream_blocks


class _FakeStream(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self._model = model
        self.teardown_called = False

    def teardown(self) -> None:
        self.teardown_called = True


class _FakeStage:
    _is_streaming = True
    _device = torch.device("cpu")
    _dtype = torch.bfloat16
    _alloc_trim_strategy = "fake"

    def _prepared_builder(self) -> object:
        return object()


def test_experimental_cpu_streaming_avoids_redundant_recursive_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_model = torch.nn.Sequential(torch.nn.Linear(2, 2))
    raw_model.eval()  # StreamingModelBuilder.build() does this upstream.
    wrapped = _FakeStream(raw_model)
    created: list[tuple[object, ...]] = []

    @contextmanager
    def fake_streaming_model(*args: object):
        created.append(args)
        try:
            yield wrapped
        finally:
            wrapped.teardown()

    monkeypatch.setattr(upstream_blocks, "_streaming_model", fake_streaming_model)

    def fail_second_eval(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("recursive eval must not run twice")

    # If the original upstream X0Model(wrapper).eval() executes, it traverses
    # this model a second time and the test fails.
    monkeypatch.setattr(raw_model, "train", fail_second_eval)
    first, second = _FakeStage(), _FakeStage()
    pipeline = SimpleNamespace(stage_1=first, stage_2=second)
    reference_a2v.configure_experimental_a2v_streaming(pipeline)

    for stage in (first, second):
        with stage._streaming_transformer_ctx() as model:
            assert model.training is False
            assert wrapped.training is False
            assert raw_model.training is False
            assert model.velocity_model is wrapped
        assert wrapped.teardown_called
    assert len(created) == 2


def test_experimental_guard_rejects_partial_patch() -> None:
    first = _FakeStage()
    second = SimpleNamespace(_is_streaming=False)
    pipeline = SimpleNamespace(stage_1=first, stage_2=second)
    with pytest.raises(RuntimeError, match="refusing partial patch"):
        reference_a2v.configure_experimental_a2v_streaming(pipeline)
    assert "_streaming_transformer_ctx" not in vars(first)


def test_experimental_guard_rejects_non_eval_meta_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapped = _FakeStream(torch.nn.Linear(2, 2))  # Still training

    @contextmanager
    def fake_streaming_model(*_args: object):
        try:
            yield wrapped
        finally:
            wrapped.teardown()

    monkeypatch.setattr(upstream_blocks, "_streaming_model", fake_streaming_model)
    pipeline = SimpleNamespace(stage_1=_FakeStage(), stage_2=_FakeStage())
    reference_a2v.configure_experimental_a2v_streaming(pipeline)
    with pytest.raises(RuntimeError, match="must already be in eval mode"):
        with pipeline.stage_1._streaming_transformer_ctx():
            pass
    assert wrapped.teardown_called
