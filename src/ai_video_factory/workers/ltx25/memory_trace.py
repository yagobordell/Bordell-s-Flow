"""Low-overhead, request-scoped RAM history for experimental I2V B=2.

CPU filesystem telemetry only: sampling never touches CUDA, synchronizes the
GPU, collects garbage, or changes any model tensor. Every sample is emitted
as structured worker logs so an OOM still leaves a partial trace. Successful
runs also persist the full timeline in the existing durable JSON report.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable

from .resource_metrics import memory_pressure

logger = logging.getLogger(__name__)

_CURRENT: ContextVar[I2VMemoryTrace | None] = ContextVar("i2v_memory_trace", default=None)

# Avoid repeated static field names (and the large cgroup memory.stat dump).
_FIELDS = (
    "process_vmrss_bytes",
    "process_vmhwm_bytes",
    "process_vmswap_bytes",
    "process_major_page_faults",
    "process_read_bytes",
    "cgroup_memory_current_bytes",
    "cgroup_memory_peak_bytes",
    "cgroup_memory_limit_bytes",
    "cgroup_swap_counter_bytes",
    "cgroup_swap_peak_bytes",
    "cgroup_anon",
    "cgroup_file",
    "cgroup_pgmajfault",
    "cgroup_memory_events_oom",
    "cgroup_memory_events_oom_kill",
    "cgroup_memory_events_high",
)


class I2VMemoryTrace:
    """Sample RSS, swap and cgroups in a small daemon thread every 2 seconds."""

    def __init__(
        self, *, interval_seconds: float = 2.0,
        reader: Callable[[], dict[str, Any]] = memory_pressure,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds < 0.5:
            raise ValueError("I2V RAM telemetry interval must be >= 0.5s")
        self.interval_seconds = interval_seconds
        self._reader = reader
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start: float | None = None
        self._phase = "start"
        self._token: Any = None
        self._samples: list[dict[str, Any]] = []
        self._markers: list[dict[str, Any]] = []
        self._errors = 0
        self._dropped_samples = 0
        self._cgroup_seen = False
        self._cgroup_resolution: str | None = None
        self._cgroup_unavailable_reason: str | None = None

    def __enter__(self) -> I2VMemoryTrace:
        self._start = self._clock()
        self._token = _CURRENT.set(self)
        self.sample_now()
        self._thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="i2v-ram-trace",
        )
        self._thread.start()
        logger.info(
            "LTX25_I2V_RAM_TRACE_START interval_seconds=%.1f cgroup_seen=%s",
            self.interval_seconds, self._cgroup_seen,
        )
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            # The worker reads a few /proc files without taking a CUDA lock.
            self._thread.join(timeout=5.0)
        self.sample_now()
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None
        logger.info(
            "LTX25_I2V_RAM_TRACE_FINISH %s",
            json.dumps(self.summary(), sort_keys=True),
        )

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample_now()

    def sample_now(self) -> None:
        if self._start is None:
            return
        try:
            data = self._reader()
            now = self._clock()
            with self._lock:
                phase = self._phase
            sample = {
                "elapsed_seconds": round(now - self._start, 3),
                "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "phase": phase,
                "process_available": bool(data.get("process_available")),
                "cgroup_available": bool(data.get("cgroup_available")),
            }
            sample.update({key: data[key] for key in _FIELDS if key in data})
            with self._lock:
                self._cgroup_seen |= bool(data.get("cgroup_available"))
                if data.get("cgroup_resolution"):
                    self._cgroup_resolution = str(data["cgroup_resolution"])
                if data.get("cgroup_unavailable_reason"):
                    self._cgroup_unavailable_reason = str(
                        data["cgroup_unavailable_reason"]
                    )
                # 3-hour smoke at 2s needs ~5400 points. Bound pathological
                # runners without suppressing live structured logs.
                if len(self._samples) < 7200:
                    self._samples.append(sample)
                else:
                    self._dropped_samples += 1
            logger.info("LTX25_I2V_RAM_SAMPLE %s", json.dumps(sample, sort_keys=True))
        except Exception as exc:
            with self._lock:
                self._errors += 1
            logger.warning(
                "LTX25_I2V_RAM_SAMPLE_FAILED exception_type=%s",
                type(exc).__name__,
            )

    def mark(self, phase: str) -> None:
        if self._start is None:
            return
        with self._lock:
            self._phase = phase
            self._markers.append({
                "phase": phase,
                "elapsed_seconds": round(self._clock() - self._start, 3),
            })
        # Capture short builds and phase boundaries that could otherwise
        # fall between two periodic samples.
        self.sample_now()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            markers = list(self._markers)
            dropped, errors = self._dropped_samples, self._errors
            cgroup_seen = self._cgroup_seen
            resolution = self._cgroup_resolution
            unavailable_reason = self._cgroup_unavailable_reason

        def maximum(key: str) -> int | None:
            values = [s[key] for s in samples if key in s]
            return max(values) if values else None

        def difference(key: str) -> int | None:
            values = [s[key] for s in samples if key in s]
            return max(0, values[-1] - values[0]) if len(values) > 1 else None

        per_phase: dict[str, dict[str, int | None]] = {}
        for phase in dict.fromkeys(s["phase"] for s in samples):
            phase_samples = [s for s in samples if s["phase"] == phase]
            per_phase[phase] = {
                key: max((s[key] for s in phase_samples if key in s), default=None)
                for key in (
                    "process_vmrss_bytes",
                    "process_vmswap_bytes",
                    "cgroup_memory_current_bytes",
                    "cgroup_swap_counter_bytes",
                )
            }
        headroom = [
            s["cgroup_memory_limit_bytes"] - s["cgroup_memory_current_bytes"]
            for s in samples
            if "cgroup_memory_limit_bytes" in s
            and "cgroup_memory_current_bytes" in s
        ]
        return {
            "schema_version": 1,
            "interval_seconds": self.interval_seconds,
            "sample_count": len(samples),
            "dropped_samples": dropped,
            "sample_errors": errors,
            "process_sampled_peak_rss_bytes": maximum("process_vmrss_bytes"),
            "process_reported_peak_hwm_bytes": maximum("process_vmhwm_bytes"),
            "process_sampled_peak_swap_bytes": maximum("process_vmswap_bytes"),
            "cgroup_available_during_run": cgroup_seen,
            "cgroup_resolution": resolution,
            "cgroup_unavailable_reason": None if cgroup_seen else unavailable_reason,
            "cgroup_sampled_peak_bytes": maximum("cgroup_memory_current_bytes"),
            "cgroup_reported_peak_bytes": maximum("cgroup_memory_peak_bytes"),
            "cgroup_min_sampled_headroom_bytes": min(headroom) if headroom else None,
            "cgroup_sampled_peak_swap_bytes": maximum("cgroup_swap_counter_bytes"),
            "process_major_fault_delta": difference("process_major_page_faults"),
            "cgroup_major_fault_delta": difference("cgroup_pgmajfault"),
            "cgroup_oom_event_delta": difference("cgroup_memory_events_oom"),
            "cgroup_oom_kill_event_delta": difference("cgroup_memory_events_oom_kill"),
            "per_phase_sampled_peaks": per_phase,
            "phase_markers": markers,
        }

    def report(self) -> dict[str, Any]:
        """Structured durable samples plus truthful sampled/kernel peak summary."""
        report = self.summary()
        with self._lock:
            report["samples"] = list(self._samples)
        return report


def mark_i2v_ram_phase(phase: str) -> None:
    """No-op outside the explicit experimental B2 request context."""
    current = _CURRENT.get()
    if current is not None:
        current.mark(phase)
