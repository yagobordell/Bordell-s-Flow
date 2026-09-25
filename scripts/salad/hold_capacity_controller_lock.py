from __future__ import annotations

import argparse
import queue
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


def _read_commands(commands: queue.Queue[str]) -> None:
    try:
        for line in sys.stdin:
            commands.put(line.strip().lower())
    finally:
        commands.put("release")


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

    commands: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=_read_commands, args=(commands,), daemon=True)
    reader.start()

    try:
        lock.assert_held()
        print("LOCK_ACQUIRED", flush=True)
        while True:
            try:
                command = commands.get(timeout=args.poll_seconds)
            except queue.Empty:
                command = "heartbeat"

            if command == "release":
                return 0
            if command not in {"check", "heartbeat", ""}:
                print(
                    f"LOCK_PROTOCOL_ERROR unsupported command: {command}",
                    file=sys.stderr,
                    flush=True,
                )
                return 5

            try:
                lock.assert_held()
            except Exception as error:
                print(
                    f"LOCK_LOST {type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 4

            if command == "check":
                print("LOCK_OK", flush=True)
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
