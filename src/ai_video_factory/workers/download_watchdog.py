from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Sequence
from pathlib import Path

_MIB = 1024 * 1024
_SALAD_IMDS_REALLOCATE_URL = "http://169.254.169.254/v1/reallocate"


def directory_size_bytes(root: Path) -> int:
    """Return the current byte size of a changing download tree."""

    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def process_write_bytes(pid: int) -> int | None:
    """Return Linux process disk write bytes when procfs exposes them."""

    try:
        text = Path(f"/proc/{pid}/io").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "write_bytes":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def request_salad_reallocation(reason: str, *, timeout_seconds: float = 3.0) -> bool:
    """Ask Salad IMDS to move this replica away from an under-performing node."""

    payload = json.dumps({"reason": reason}).encode("utf-8")
    request = urllib.request.Request(
        _SALAD_IMDS_REALLOCATE_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Metadata": "true"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            success = 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False
    return success


def run_with_progress_watchdog(
    command: Sequence[str],
    *,
    progress_root: Path,
    stall_timeout_seconds: float,
    hard_timeout_seconds: float,
    poll_seconds: float,
    label: str = "model",
    min_throughput_mib_per_second: float = 0.0,
    throughput_grace_seconds: float = 180.0,
    throughput_window_seconds: float = 120.0,
    reallocate_on_slow: bool = False,
    min_progress_reset_bytes: int = 1,
) -> int:
    """Run a downloader with byte-progress, throughput, and absolute deadlines."""

    if not command:
        raise ValueError("download watchdog requires a command")
    if stall_timeout_seconds <= 0 or hard_timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("download watchdog timeouts and poll interval must be positive")
    if stall_timeout_seconds >= hard_timeout_seconds:
        raise ValueError("stall timeout must be lower than the hard timeout")
    if min_throughput_mib_per_second < 0:
        raise ValueError("minimum throughput cannot be negative")
    if min_progress_reset_bytes <= 0:
        raise ValueError("minimum progress reset bytes must be positive")
    if throughput_grace_seconds < 0 or throughput_window_seconds <= 0:
        raise ValueError("throughput grace/window values are invalid")

    progress_root.mkdir(parents=True, exist_ok=True)
    initial_tree_bytes = directory_size_bytes(progress_root)
    process = subprocess.Popen(list(command), start_new_session=True)
    initial_process_write_bytes = process_write_bytes(process.pid)

    started = time.monotonic()
    last_progress_at = started
    last_progress_bytes = 0
    last_meaningful_progress_bytes = 0
    samples: deque[tuple[float, int]] = deque([(started, 0)])
    print(
        f"MODEL_DOWNLOAD_WATCHDOG label={label} initial_tree_bytes={initial_tree_bytes} "
        f"stall_timeout_seconds={stall_timeout_seconds:g} "
        f"hard_timeout_seconds={hard_timeout_seconds:g} "
        f"min_throughput_mibps={min_throughput_mib_per_second:g}",
        flush=True,
    )

    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                return return_code

            time.sleep(poll_seconds)
            now = time.monotonic()
            current_tree_bytes = directory_size_bytes(progress_root)
            tree_progress = max(0, current_tree_bytes - initial_tree_bytes)
            current_process_write_bytes = process_write_bytes(process.pid)
            io_progress = 0
            if (
                current_process_write_bytes is not None
                and initial_process_write_bytes is not None
            ):
                io_progress = max(
                    0,
                    current_process_write_bytes - initial_process_write_bytes,
                )

            current_progress_bytes = max(last_progress_bytes, tree_progress, io_progress)
            delta = current_progress_bytes - last_progress_bytes
            if delta > 0:
                last_progress_bytes = current_progress_bytes
            meaningful_delta = (
                current_progress_bytes - last_meaningful_progress_bytes
            )
            if meaningful_delta >= min_progress_reset_bytes:
                last_meaningful_progress_bytes = current_progress_bytes
                last_progress_at = now

            samples.append((now, current_progress_bytes))
            window_start = now - throughput_window_seconds
            while len(samples) > 2 and samples[1][0] <= window_start:
                samples.popleft()

            elapsed = now - started
            idle = now - last_progress_at
            throughput_mibps = _throughput_mib_per_second(samples)
            process_write_rendered = (
                str(current_process_write_bytes)
                if current_process_write_bytes is not None
                else "-"
            )
            print(
                f"MODEL_DOWNLOAD_PROGRESS label={label} elapsed_seconds={elapsed:.1f} "
                f"progress_bytes={current_progress_bytes} delta_bytes={delta} "
                f"tree_bytes={current_tree_bytes} "
                f"process_write_bytes={process_write_rendered} "
                f"meaningful_delta_bytes={meaningful_delta} "
                f"idle_seconds={idle:.1f} window_mibps={throughput_mibps:.2f}",
                flush=True,
            )

            if elapsed >= hard_timeout_seconds:
                _terminate_process_group(process)
                raise TimeoutError(
                    f"{label} download exceeded hard timeout of {hard_timeout_seconds:g} seconds"
                )
            if idle >= stall_timeout_seconds:
                _terminate_process_group(process)
                reason = (
                    f"{label} model download made no meaningful byte progress "
                    f"(minimum reset={min_progress_reset_bytes} bytes) for "
                    f"{stall_timeout_seconds:g} seconds"
                )
                if reallocate_on_slow:
                    _request_reallocation_with_log(reason)
                raise TimeoutError(reason)

            if (
                min_throughput_mib_per_second > 0
                and elapsed >= throughput_grace_seconds
                and _sample_span_seconds(samples) >= throughput_window_seconds * 0.8
                and throughput_mibps < min_throughput_mib_per_second
            ):
                _terminate_process_group(process)
                reason = (
                    f"{label} model download throughput {throughput_mibps:.2f} MiB/s is below "
                    f"required {min_throughput_mib_per_second:.2f} MiB/s"
                )
                if reallocate_on_slow:
                    _request_reallocation_with_log(reason)
                raise TimeoutError(reason)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)


def _sample_span_seconds(samples: deque[tuple[float, int]]) -> float:
    if len(samples) < 2:
        return 0.0
    return max(0.0, samples[-1][0] - samples[0][0])


def _throughput_mib_per_second(samples: deque[tuple[float, int]]) -> float:
    elapsed = _sample_span_seconds(samples)
    if elapsed <= 0:
        return 0.0
    transferred = max(0, samples[-1][1] - samples[0][1])
    return transferred / _MIB / elapsed


def _request_reallocation_with_log(reason: str) -> None:
    print(f"SALAD_REALLOCATION_REQUEST reason={reason}", flush=True)
    if request_salad_reallocation(reason):
        print("SALAD_REALLOCATION_REQUESTED", flush=True)
    else:
        print("SALAD_REALLOCATION_REQUEST_FAILED", flush=True)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            return
        process.wait(timeout=10)
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=10)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch a model download for progress and speed.")
    parser.add_argument("--progress-root", type=Path, required=True)
    parser.add_argument("--stall-timeout-seconds", type=float, required=True)
    parser.add_argument("--hard-timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--label", default="model")
    parser.add_argument("--min-throughput-mibps", type=float, default=0.0)
    parser.add_argument("--throughput-grace-seconds", type=float, default=180.0)
    parser.add_argument("--throughput-window-seconds", type=float, default=120.0)
    parser.add_argument("--reallocate-on-slow", action="store_true")
    parser.add_argument("--min-progress-reset-bytes", type=int, default=1)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args


def main() -> None:
    args = parse_args()
    try:
        return_code = run_with_progress_watchdog(
            args.command,
            progress_root=args.progress_root,
            stall_timeout_seconds=args.stall_timeout_seconds,
            hard_timeout_seconds=args.hard_timeout_seconds,
            poll_seconds=args.poll_seconds,
            label=args.label,
            min_throughput_mib_per_second=args.min_throughput_mibps,
            throughput_grace_seconds=args.throughput_grace_seconds,
            throughput_window_seconds=args.throughput_window_seconds,
            reallocate_on_slow=args.reallocate_on_slow,
            min_progress_reset_bytes=args.min_progress_reset_bytes,
        )
    except (TimeoutError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if return_code != 0:
        raise SystemExit(return_code)


if __name__ == "__main__":
    main()
