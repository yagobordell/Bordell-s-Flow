from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from ai_video_factory.inference.salad_capacity import build_salad_capacity_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile Salad Container Group replicas from the canonical Postgres gpu.jobs queue."
        )
    )
    parser.add_argument(
        "--services",
        type=Path,
        default=REPO_ROOT / "deploy" / "salad" / "services.json",
    )
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    args = parse_args()
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")

    runtime = build_salad_capacity_runtime(
        services_path=args.services,
        postgres_dsn=_required_env("POSTGRES_DSN"),
        salad_api_key=_required_env("SALAD_API_KEY"),
    )
    if not runtime.autoscaler.config.enabled:
        runtime.close()
        raise SystemExit(
            "SALAD_AUTOSCALER_ENABLED is false; refusing to run a no-op capacity controller"
        )

    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        runtime.store.ping()
        print(
            "Salad capacity controller started: Postgres demand is the sole replica authority.",
            flush=True,
        )
        while not stop.is_set():
            try:
                runtime.autoscaler.reconcile()
            except Exception as error:
                print(
                    f"Salad capacity reconciliation deferred after transient error: {error}",
                    flush=True,
                )
                if args.once:
                    raise
            if args.once:
                return
            stop.wait(args.poll_seconds)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
