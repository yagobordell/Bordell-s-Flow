"""Experimental A2V Gemma timings without changing tokenizer/model calculations.

The pinned upstream text-encoder context constructs and transfers weights
inside one context-enter operation. We time that combined phase accurately;
we do NOT claim to distinguish loading from H2D without deeper upstream
instrumentation. All instance overrides are restored, including on failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .i2v_metrics import I2VPhaseMetrics


@contextmanager
def profile_a2v_prompt_encoder(
    encoder: Any, metrics: I2VPhaseMetrics
) -> Iterator[None]:
    missing = object()
    instance_vars = vars(encoder)
    prior_ctx = instance_vars.get("_text_encoder_ctx", missing)
    prior_build = instance_vars.get("_build_embeddings_processor", missing)
    original_ctx = encoder._text_encoder_ctx
    original_build = encoder._build_embeddings_processor

    class TimedContext:
        def __init__(self, context: Any) -> None:
            self.context = context

        def __enter__(self) -> Any:
            with metrics.phase("prompt_model_acquire_seconds"):
                return self.context.__enter__()

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
            with metrics.phase("prompt_model_release_seconds"):
                return self.context.__exit__(exc_type, exc, traceback)

    def timed_context() -> TimedContext:
        return TimedContext(original_ctx())

    def timed_build() -> Any:
        with metrics.phase("prompt_embeddings_processor_build_seconds"):
            return original_build()

    encoder._text_encoder_ctx = timed_context
    encoder._build_embeddings_processor = timed_build
    try:
        yield
    finally:
        if prior_ctx is missing:
            instance_vars.pop("_text_encoder_ctx", None)
        else:
            encoder._text_encoder_ctx = prior_ctx
        if prior_build is missing:
            instance_vars.pop("_build_embeddings_processor", None)
        else:
            encoder._build_embeddings_processor = prior_build
