"""Optional no-tensor-mutation profiler for isolated fast I2V prompt encoding.

Profiles actual Gemma streaming/load/encode and embeddings-processor build,
then restores the original bound methods in a finally block. The worker's
existing mode lock serializes all calls to the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .i2v_metrics import I2VPhaseMetrics


@contextmanager
def profile_prompt_encoder(
    encoder: Any, metrics: I2VPhaseMetrics
) -> Iterator[None]:
    original_ctx = encoder._text_encoder_ctx
    original_embeddings_build = encoder._build_embeddings_processor

    def timed_text_ctx() -> Any:
        @contextmanager
        def inner() -> Iterator[Any]:
            with metrics.phase("prompt_text_encoder_load_and_encode_seconds"):
                with original_ctx() as text_encoder:
                    yield text_encoder

        return inner()

    def timed_embeddings_build() -> Any:
        with metrics.phase("prompt_embeddings_processor_build_seconds"):
            return original_embeddings_build()

    encoder._text_encoder_ctx = timed_text_ctx
    encoder._build_embeddings_processor = timed_embeddings_build
    try:
        yield
    finally:
        encoder._text_encoder_ctx = original_ctx
        encoder._build_embeddings_processor = original_embeddings_build
