"""Opt-in A2V block-streaming guard; no GPU imports until first real use.

Pinned LTX upstream builds its meta model in eval mode before attaching
streaming hooks. Repeating X0Model(wrapper).eval() recursively traverses the
live module hierarchy a second time; the third controlled warm run failed
during that redundant traversal. Keep upstream build/teardown unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import MethodType
from typing import Any


@contextmanager
def _experimental_streaming_transformer_context(stage: Any):
    from ltx_core.model.transformer import X0Model
    from ltx_pipelines.utils.blocks import _streaming_model

    if not stage._is_streaming:
        raise RuntimeError("experimental streaming guard requires a streaming stage")
    builder = stage._prepared_builder()
    with _streaming_model(
        builder, stage._device, stage._dtype, stage._alloc_trim_strategy
    ) as streaming_wrapper:
        raw_model = streaming_wrapper._model
        if raw_model.training:
            raise RuntimeError("A2V streaming meta model must already be in eval mode")
        # Do not call .eval() recursively after live streaming hooks were set up:
        # StreamingModelBuilder.build() already evaluated the raw model.
        streaming_wrapper.training = False
        model = X0Model(streaming_wrapper)
        model.training = False
        yield model


def configure_experimental_a2v_streaming(pipeline: Any) -> None:
    """Patch both stages atomically, only when the benchmark explicitly asks."""
    for name in ("stage_1", "stage_2"):
        stage = getattr(pipeline, name)
        if not getattr(stage, "_is_streaming", False):
            raise RuntimeError(
                f"Experimental CPU A2V expected streaming {name}; refusing partial patch"
            )
    for name in ("stage_1", "stage_2"):
        stage = getattr(pipeline, name)
        stage._streaming_transformer_ctx = MethodType(
            _experimental_streaming_transformer_context, stage
        )
