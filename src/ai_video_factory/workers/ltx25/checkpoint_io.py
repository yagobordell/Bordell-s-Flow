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
import uuid
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


def probe_bootstrap_storage(
    root: Path, *, minimum_mibps: float,
    sample_bytes: int = _DIRECT_BLOCK_SIZE,
    write_blocks: int = 32,
) -> dict[str, Any]:
    """Early opt-in screening on the actual model filesystem, before downloads.

    A bounded scratch file is written with O_DIRECT, fsynced, sampled by the
    checkpoint's existing O_DIRECT reader, and removed in all cases. Sampling
    fresh scratch storage cannot prove that a downloaded checkpoint will load
    quickly; the *second*, real-checkpoint probe is therefore mandatory.
    """
    if not 0 < minimum_mibps <= 10000:
        raise ValueError("Invalid experimental bootstrap storage minimum")
    if sample_bytes < 4096 or sample_bytes % 4096 or not 10 <= write_blocks <= 128:
        raise ValueError("Invalid bootstrap storage geometry")
    if not all(hasattr(os, flag) for flag in ("O_DIRECT", "pwritev", "preadv")):
        raise RuntimeError("A2V bootstrap storage requires Linux direct I/O")
    root.mkdir(parents=True, exist_ok=True)
    scratch = root / f".a2v-direct-probe-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(
            scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_DIRECT, 0o600,
        )
        try:
            with mmap.mmap(-1, sample_bytes) as buffer:
                aligned = memoryview(buffer)
                try:
                    for index in range(write_blocks):
                        aligned[:] = os.urandom(sample_bytes)
                        written = os.pwritev(fd, [aligned], index * sample_bytes)
                        if written != sample_bytes:
                            raise RuntimeError(
                                f"A2V short scratch write: {written}/{sample_bytes}"
                            )
                finally:
                    aligned.release()
            os.fsync(fd)
        finally:
            os.close(fd)
            fd = None
        result = probe_checkpoint_direct_read(
            scratch, minimum_mibps=minimum_mibps, sample_bytes=sample_bytes,
            samples=min(16, write_blocks - 1),
        )
        result["probe_kind"] = "bootstrap_scratch_screen"
        result["scratch_bytes"] = sample_bytes * write_blocks
        return result
    finally:
        if fd is not None:
            os.close(fd)
        scratch.unlink(missing_ok=True)


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Strict experimental LTX A2V storage gate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bootstrap-root", type=Path)
    mode.add_argument("--checkpoint", type=Path)
    parser.add_argument("--minimum-mibps", type=float, required=True)
    parser.add_argument("--reallocate-on-slow", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            probe_bootstrap_storage(
                args.bootstrap_root, minimum_mibps=args.minimum_mibps,
            )
            if args.bootstrap_root is not None else
            probe_checkpoint_direct_read(
                args.checkpoint, minimum_mibps=args.minimum_mibps,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            "LTX25_EXPERIMENTAL_STORAGE_PREFLIGHT_FAILED "
            f"phase={'bootstrap' if args.bootstrap_root is not None else 'checkpoint'} "
            f"reason={exc}",
            flush=True,
        )
        if args.reallocate_on_slow:
            from ai_video_factory.workers.download_watchdog import (
                request_salad_reallocation,
            )

            reason = (
                "A2V experimental storage too slow/unsupported for sub-200s trials: "
                + str(exc)[:180]
            )
            print(f"SALAD_REALLOCATION_REQUEST reason={reason}", flush=True)
            if request_salad_reallocation(reason):
                print("SALAD_REALLOCATION_REQUESTED", flush=True)
            else:
                print("SALAD_REALLOCATION_REQUEST_FAILED", flush=True)
        raise SystemExit(1) from exc
    print(
        "LTX25_EXPERIMENTAL_STORAGE_PREFLIGHT_OK " + json.dumps(result, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
