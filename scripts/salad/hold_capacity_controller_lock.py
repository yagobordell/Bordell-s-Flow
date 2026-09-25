from __future__ import annotations

import argparse
import sys
import threading

from ai_video_factory.inference.capacity_controller import (
    CapacityControllerLeadershipError,
    PostgresCapacityControllerOperationLock,
    resolve_capacity_controller_dsn,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=1.0,
        help="How often to verify that the PostgreSQL advisory lock is still held.",
    )
    return parser.parse_args()


def _wait_for_release(release_requested: threading.Event) -> None:
    try:
        sys.stdin.readline()
    finally:
        release_requested.set()


def main() -> int:
    args = _parse_args()
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")

    lock: PostgresCapacityControllerOperationLock | None = None
    try:
        lock = PostgresCapacityControllerOperationLock(
            resolve_capacity_controller_dsn()
        )
    except CapacityControllerLeadershipError as error:
        print("LOCK_HELD", flush=True)
        print(str(error), file=sys.stderr, flush=True)
        return 3
    except Exception as error:
        print("LOCK_ERROR", flush=True)
        print(f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2

    release_requested = threading.Event()
    release_thread = threading.Thread(
        target=_wait_for_release,
        args=(release_requested,),
        daemon=True,
    )
    release_thread.start()

    try:
        lock.assert_held()
        print("LOCK_ACQUIRED", flush=True)
        while not release_requested.wait(timeout=args.poll_seconds):
            try:
                lock.assert_held()
            except Exception as error:
                print(
                    f"LOCK_LOST {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 4
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
