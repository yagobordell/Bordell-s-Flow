from __future__ import annotations

import sys

from ai_video_factory.inference.capacity_controller import (
    CapacityControllerLeadershipError,
    PostgresCapacityControllerOperationLock,
    resolve_capacity_controller_dsn,
)


def main() -> int:
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

    try:
        print("LOCK_ACQUIRED", flush=True)
        sys.stdin.readline()
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
