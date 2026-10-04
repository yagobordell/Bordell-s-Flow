"""Reuse one unchanged upstream VAE build across two *separate* I2V decodes.

Experimental Full HD I2V only. Preserve each original lazy decode iterator,
latent, VAE tiling and the actual advanced per-video RNG generator; never stack
the clips or seed new generators. The caller owns this session and must close
it in a finally block before releasing the pipeline lock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class SharedSequentialVideoDecoder:
    """The VideoDecoder call contract with one upstream decoder build for B=1+B=1."""

    def __init__(self, original: Any) -> None:
        self._original = original
        self._model: Any | None = None
        self._model_context: Any | None = None
        self._active_iterator: Iterator[Any] | None = None
        self._busy = False
        self._closed = False
        self._completed = 0
        self._build_seconds = 0.0

    def __call__(
        self,
        latent: Any,
        tiling_config: Any = None,
        generator: Any = None,
        *,
        dtype: Any = None,
    ) -> Iterator[Any]:
        # Return a lazy generator just like the original VideoDecoder. Model
        # construction must wait until the actual MP4 encoder consumes it.
        return self._decode(latent, tiling_config, generator, dtype)

    def _decode(
        self, latent: Any, tiling_config: Any, generator: Any, dtype: Any,
    ) -> Iterator[Any]:
        if self._closed or self._busy or self._completed >= 2:
            raise RuntimeError("I2V shared VAE requires exactly two sequential full decodes")
        self._busy = True
        started = time.perf_counter()
        try:
            if self._model is None:
                from ltx_pipelines.utils.gpu_model import gpu_model

                original = self._original
                build_dtype = original._dtype if dtype is None else dtype
                model = original._prepared_builder().build(
                    device=original._device, dtype=build_dtype,
                ).eval()
                # Exactly the same upstream disposal context as _cleanup_iter,
                # retained until the second original decode finishes.
                context = gpu_model(
                    model, alloc_trim_strategy=original._alloc_trim_strategy,
                )
                self._model_context = context
                self._model = context.__enter__()
                self._build_seconds = time.perf_counter() - started
                logger.info(
                    "LTX25_I2V_SHARED_VAE_BUILD seconds=%.3f", self._build_seconds,
                )
            build_dtype = self._original._dtype if dtype is None else dtype
            self._active_iterator = iter(
                self._model.decode_video(
                    latent.to(dtype=build_dtype),
                    tiling_config,
                    generator,
                )
            )
            yield from self._active_iterator
            self._active_iterator = None
            self._completed += 1
            logger.info(
                "LTX25_I2V_SHARED_VAE_CLIP_DONE clip=%d "
                "decode_plus_encoder_consumption_seconds=%.3f",
                self._completed, time.perf_counter() - started,
            )
        except BaseException:
            self.close()
            raise
        finally:
            self._busy = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # If the encoder aborted without consuming the lazy generator, close
        # it explicitly before leaving the shared model's disposal context.
        active = self._active_iterator
        self._active_iterator = None
        if active is not None:
            close = getattr(active, "close", None)
            if close is not None:
                close()
        context = self._model_context
        self._model_context = None
        self._model = None
        if context is not None:
            context.__exit__(None, None, None)
        logger.info(
            "LTX25_I2V_SHARED_VAE_RELEASE completed=%d build_seconds=%.3f",
            self._completed, self._build_seconds,
        )

    @property
    def completed(self) -> int:
        return self._completed

    def __enter__(self) -> SharedSequentialVideoDecoder:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
