"""Opt-in CUDA-synchronized per-phase timing for the LTX I2V worker.

Decode yields chunks lazily, so video timing must wrap iterator consumption,
not just VideoDecoder.__call__. Instrumentation runs only when explicitly
requested and does not mutate tensors, RNG or pipeline components.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class I2VPhaseMetrics:
    def __init__(self, torch_module: Any, device: Any) -> None:
        self._cuda = getattr(torch_module, "cuda", None)
        self._device = device
        self._is_cuda = str(device).startswith("cuda") and bool(
            self._cuda is not None and self._cuda.is_available()
        )
        self.timings: dict[str, float] = {}
        self.cuda_peak_reset = False
        self._process_cpu_started = time.process_time()

    def begin_generation(self) -> None:
        """Reset allocator peaks before this request, without changing tensors or RNG.

        Do not fail a video job if the installed torch backend lacks this
        instrumentation. The metric explicitly reports whether reset worked.
        """
        self._process_cpu_started = time.process_time()
        if not self._is_cuda:
            return
        reset = getattr(self._cuda, "reset_peak_memory_stats", None)
        if not callable(reset):
            return
        try:
            self._synchronize()
            reset(self._device)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning("LTX25_I2V_CUDA_PEAK_RESET_UNAVAILABLE error=%s", exc)
        else:
            self.cuda_peak_reset = True

    def _synchronize(self) -> None:
        if self._is_cuda:
            synchronize = getattr(self._cuda, "synchronize", None)
            if synchronize is not None:
                try:
                    synchronize()
                except (RuntimeError, TypeError, ValueError) as exc:
                    # Diagnostic timing must not invalidate a paid inference.
                    logger.warning("LTX25_I2V_SYNC_METRIC_UNAVAILABLE error=%s", exc)

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

    def diagnostic_snapshot(self) -> dict[str, float]:
        """Lightweight post-job CPU/RAM/CUDA snapshot; no background sampling.

        Peak RSS is a process lifetime high-water mark (not a per-job peak).
        Host available memory is a point-in-time Linux /proc measurement.
        Missing values remain absent rather than being inferred as zero.
        """
        snapshot: dict[str, float] = {
            "process_cpu_seconds": round(
                max(time.process_time() - self._process_cpu_started, 0.0), 3
            ),
        }
        try:
            meminfo = Path("/proc/meminfo")
            if meminfo.is_file():
                for line in meminfo.read_text(encoding="ascii").splitlines():
                    name, _, raw = line.partition(":")
                    if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                        snapshot[{
                            "MemTotal": "host_ram_total_mb",
                            "MemAvailable": "host_ram_available_mb",
                            "SwapTotal": "host_swap_total_mb",
                            "SwapFree": "host_swap_free_mb",
                        }[name]] = round(int(raw.strip().split()[0]) / 1024, 2)
        except (OSError, ValueError, IndexError):
            logger.debug("LTX25_I2V_HOST_MEMORY_METRICS_UNAVAILABLE")

        try:
            import resource

            snapshot["process_peak_rss_mb"] = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
            )
        except (ImportError, OSError, ValueError):
            pass

        if self._is_cuda:
            for name, fn_name in (
                ("cuda_allocated_mb", "memory_allocated"),
                ("cuda_reserved_mb", "memory_reserved"),
                ("cuda_peak_allocated_mb", "max_memory_allocated"),
                ("cuda_peak_reserved_mb", "max_memory_reserved"),
            ):
                fn = getattr(self._cuda, fn_name, None)
                if callable(fn):
                    try:
                        snapshot[name] = round(fn() / (1024 * 1024), 2)
                    except (RuntimeError, TypeError, ValueError):
                        logger.debug("LTX25_I2V_MEMORY_METRIC_UNAVAILABLE %s", name)
        return snapshot

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
