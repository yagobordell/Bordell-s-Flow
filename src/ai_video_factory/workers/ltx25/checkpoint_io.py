"""Experimental A2V cold checkpoint read probe; no GPU and no cache pollution.

Only used when explicitly requested by the isolated paired A2V task. Uses
Linux O_DIRECT to bypass the OS page cache: the preceding 70-GiB HuggingFace
bootstrap may leave parts of the checkpoint hot in memory, concealing a slow
actual storage device. A failed/unsupported probe rejects an expensive
trial; it must never silently pretend a cached read proves SSD speed.
"""

from __future__ import annotations

import mmap
import os
import statistics
import time
from pathlib import Path
from typing import Any

_DIRECT_BLOCK_SIZE = 16 * 1024 * 1024
_DIRECT_SAMPLES = 8


def probe_checkpoint_direct_read(
    path: Path, *, minimum_mibps: float = 0.0,
    sample_bytes: int = _DIRECT_BLOCK_SIZE, samples: int = _DIRECT_SAMPLES,
) -> dict[str, Any]:
    """Sample multiple distant aligned ranges with O_DIRECT.

    No file mutation, GPU calls, checksum shortcuts, hidden cached fallback
    or assumptions about cgroup memory. The operator-selected floor is an
    admission gate rather than a measured prediction of total A2V latency.
    """
    if not 0 <= minimum_mibps <= 10000:
        raise ValueError("Invalid A2V checkpoint I/O minimum")
    if samples < 2 or samples > 32 or sample_bytes < 4096 or sample_bytes % 4096:
        raise ValueError("Invalid A2V O_DIRECT sample geometry")
    if not hasattr(os, "O_DIRECT") or not hasattr(os, "preadv"):
        raise RuntimeError("A2V cold-read probe needs Linux O_DIRECT and preadv")
    size = path.stat().st_size
    if size < sample_bytes * (samples + 1):
        raise ValueError("Checkpoint too small for representative direct-read probe")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
    timings: list[float] = []
    try:
        # Anonymous mmap gives page-aligned writable buffers for O_DIRECT.
        with mmap.mmap(-1, sample_bytes) as aligned:
            view = memoryview(aligned)
            try:
                for index in range(samples):
                    offset = ((size - sample_bytes) * (index + 1) //
                              (samples + 1) // 4096) * 4096
                    start = time.perf_counter()
                    try:
                        count = os.preadv(fd, [view], offset)
                    except OSError as exc:
                        raise RuntimeError(
                            "A2V direct checkpoint read is unsupported or failed "
                            f"({exc}); refusing to rely on cached performance"
                        ) from exc
                    elapsed = time.perf_counter() - start
                    if count != sample_bytes:
                        raise RuntimeError(
                            f"A2V short direct checkpoint read: {count}/{sample_bytes}"
                        )
                    timings.append(max(elapsed, 1e-9))
            finally:
                view.release()
    finally:
        os.close(fd)
    speeds = [sample_bytes / duration / 1048576 for duration in timings]
    speed_median = statistics.median(speeds)
    result: dict[str, Any] = {
        "direct_io": True,
        "sample_bytes": sample_bytes,
        "samples": samples,
        "speed_mibps_median": round(speed_median, 2),
        "speed_mibps_min": round(min(speeds), 2),
        "speed_mibps_max": round(max(speeds), 2),
        "minimum_mibps": minimum_mibps,
    }
    if speed_median < minimum_mibps:
        raise RuntimeError(
            f"A2V checkpoint direct I/O too slow: median={speed_median:.1f} "
            f"MiB/s < required={minimum_mibps:.1f} MiB/s; "
            "do not pay for inference on this instance"
        )
    return result
