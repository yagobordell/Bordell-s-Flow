from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path


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


def run_with_progress_watchdog(
    command: Sequence[str],
    *,
    progress_root: Path,
    stall_timeout_seconds: float,
    hard_timeout_seconds: float,
    poll_seconds: float,
) -> int:
    """Run a downloader while enforcing byte-progress and absolute deadlines."""

    if not command:
        raise ValueError("download watchdog requires a command")
    if stall_timeout_seconds <= 0 or hard_timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("download watchdog timeouts and poll interval must be positive")
    if stall_timeout_seconds >= hard_timeout_seconds:
        raise ValueError("stall timeout must be lower than the hard timeout")

    progress_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    last_progress_at = started
    last_size = directory_size_bytes(progress_root)
    print(
        "Ideogram download watchdog started: "
        f"bytes={last_size} stall_timeout={stall_timeout_seconds}s "
        f"hard_timeout={hard_timeout_seconds}s",
        flush=True,
    )

    process = subprocess.Popen(list(command), start_new_session=True)
    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                return return_code

            now = time.monotonic()
            current_size = directory_size_bytes(progress_root)
            if current_size > last_size:
                delta = current_size - last_size
                last_size = current_size
                last_progress_at = now
                print(
                    "Ideogram download progress: "
                    f"bytes={current_size} delta={delta}",
                    flush=True,
                )

            if now - started >= hard_timeout_seconds:
                _terminate_process_group(process)
                raise TimeoutError(
                    "Ideogram model download exceeded hard timeout of "
                    f"{hard_timeout_seconds} seconds"
                )
            if now - last_progress_at >= stall_timeout_seconds:
                _terminate_process_group(process)
                raise TimeoutError(
                    "Ideogram model download made no byte progress for "
                    f"{stall_timeout_seconds} seconds"
                )

            time.sleep(poll_seconds)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
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
    parser = argparse.ArgumentParser(description="Watch an Ideogram model download for progress.")
    parser.add_argument("--progress-root", type=Path, required=True)
    parser.add_argument("--stall-timeout-seconds", type=float, required=True)
    parser.add_argument("--hard-timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
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
        )
    except (TimeoutError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if return_code != 0:
        raise SystemExit(return_code)


if __name__ == "__main__":
    main()
