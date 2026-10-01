"""Opt-in CUDA-synchronized per-phase timing for the LTX I2V worker.

Decode yields chunks lazily, so video timing must wrap iterator consumption,
not just VideoDecoder.__call__. Instrumentation runs only when explicitly
requested and does not mutate tensors, RNG or pipeline components.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class I2VPhaseMetrics:
    def __init__(self, torch_module: Any, device: Any) -> None:
        self._cuda = getattr(torch_module, "cuda", None)
        self._is_cuda = str(device).startswith("cuda") and bool(
            self._cuda is not None and self._cuda.is_available()
        )
        self.timings: dict[str, float] = {}

    def _synchronize(self) -> None:
        if self._is_cuda:
            synchronize = getattr(self._cuda, "synchronize", None)
            if synchronize is not None:
                synchronize()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        self._synchronize()
        start = time.perf_counter()
        try:
            yield
        finally:
            self._synchronize()
            elapsed = time.perf_counter() - start
            self.timings[name] = self.timings.get(name, 0.0) + elapsed

    def video_chunks(self, chunks: Iterator[Any]) -> Iterator[Any]:
        """Measure actual lazy DiffVAE work when the encoder pulls each chunk."""
        source = iter(chunks)
        while True:
            with self.phase("video_decode_seconds"):
                try:
                    chunk = next(source)
                except StopIteration:
                    return
            yield chunk

    def cuda_peak(self) -> dict[str, float]:
        if not self._is_cuda:
            return {}
        peak_allocated = getattr(self._cuda, "max_memory_allocated", None)
        peak_reserved = getattr(self._cuda, "max_memory_reserved", None)
        return {
            key: round(fn() / (1024 * 1024), 2)
            for key, fn in (
                ("cuda_peak_allocated_mb", peak_allocated),
                ("cuda_peak_reserved_mb", peak_reserved),
            )
            if callable(fn)
        }
