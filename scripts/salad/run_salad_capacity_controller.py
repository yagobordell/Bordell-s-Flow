from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

import psycopg

from ai_video_factory.inference.capacity_controller import (
    CapacityControllerLeadershipError,
    PostgresCapacityControllerLeadership,
    read_capacity_controller_health,
)
from ai_video_factory.inference.salad_capacity import build_salad_capacity_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run or health-check the singleton Salad capacity controller backed by Postgres."
        )
    )
    parser.add_argument(
        "--services",
        type=Path,
        default=REPO_ROOT / "deploy" / "salad" / "services.json",
    )
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=120.0)
    parser.add_argument("--max-reconcile-age-seconds", type=float, default=120.0)
    return parser.parse_args()


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _validate_autoscaling_schema(postgres_dsn: str) -> None:
    try:
        with psycopg.connect(postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT started_at, finished_at FROM gpu.jobs LIMIT 0")
                cursor.execute(
                    """
                    SELECT controller_name, controller_id, last_heartbeat_at
                    FROM gpu.capacity_controller_state
                    LIMIT 0
                    """
                )
    except psycopg.Error as error:
        raise SystemExit(
            "Predictive Salad autoscaling requires infra/sql/003_gpu_job_runtime_autoscaling.sql "
            "and infra/sql/004_salad_capacity_controller_coordination.sql before production."
        ) from error


def _run_health_check(args: argparse.Namespace, postgres_dsn: str) -> None:
    try:
        health = read_capacity_controller_health(
            postgres_dsn,
            max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
            max_reconcile_age_seconds=args.max_reconcile_age_seconds,
        )
    except psycopg.Error as error:
        raise SystemExit(f"Capacity controller health query failed: {error}") from error

    detail = (
        f"controller_id={health.controller_id or '<none>'} "
        f"status={health.status or '<none>'} "
        f"heartbeat_age={health.heartbeat_age_seconds} "
        f"reconcile_age={health.reconcile_age_seconds} "
        f"reason={health.reason}"
    )
    if not health.healthy:
        raise SystemExit(f"Salad capacity controller unhealthy: {detail}")
    print(f"Salad capacity controller healthy: {detail}", flush=True)


def main() -> None:
    args = parse_args()
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")
    if args.max_heartbeat_age_seconds <= 0 or args.max_reconcile_age_seconds <= 0:
        raise SystemExit("capacity controller health thresholds must be positive")

    postgres_dsn = _required_env("POSTGRES_DSN")
    _validate_autoscaling_schema(postgres_dsn)

    if args.check_health:
        _run_health_check(args, postgres_dsn)
        return

    runtime = None
    leadership = None
    try:
        try:
            leadership = PostgresCapacityControllerLeadership(postgres_dsn)
        except CapacityControllerLeadershipError as error:
            raise SystemExit(str(error)) from error

        runtime = build_salad_capacity_runtime(
            services_path=args.services,
            postgres_dsn=postgres_dsn,
            salad_api_key=_required_env("SALAD_API_KEY"),
        )
        if not runtime.autoscaler.config.enabled:
            raise SystemExit(
                "SALAD_AUTOSCALER_ENABLED is false; refusing to run a no-op capacity controller"
            )

        stop = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

        runtime.store.ping()
        leadership.heartbeat()
        print(
            "Salad capacity controller leader started: Postgres demand owns project replicas.",
            flush=True,
        )
        while not stop.is_set():
            leadership.heartbeat()
            try:
                runtime.autoscaler.reconcile()
            except Exception as error:
                leadership.record_failure(error)
                print(
                    f"Salad capacity reconciliation degraded after error: {error}",
                    flush=True,
                )
                if args.once:
                    raise
            else:
                leadership.record_success()
            if args.once:
                return
            stop.wait(args.poll_seconds)
    finally:
        if runtime is not None:
            runtime.close()
        if leadership is not None:
            leadership.close()


if __name__ == "__main__":
    main()
