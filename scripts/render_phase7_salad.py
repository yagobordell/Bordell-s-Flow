from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

REQUIRED_PRODUCTION_ENV = (
    "POSTGRES_DSN",
    "R2_ENDPOINT_URL",
    "R2_BUCKET",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _required_environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_PRODUCTION_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_PRODUCTION_ENV}


def _write_json(path: Path, document: dict[str, Any], *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if secret:
        path.chmod(0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render Salad queue and container-group JSON for the Phase 7 worker."
    )
    parser.add_argument("--image", required=True, help="Immutable registry image reference")
    parser.add_argument("--queue-name", default="ai-video-factory-jobs")
    parser.add_argument("--container-name", default="ai-video-factory-worker")
    parser.add_argument("--display-name", default="AI Video Factory Worker")
    parser.add_argument("--gpu-class", action="append", default=[])
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=8192)
    parser.add_argument("--replicas", type=int, default=0)
    parser.add_argument("--min-replicas", type=int, default=0)
    parser.add_argument("--max-replicas", type=int, default=1)
    parser.add_argument("--desired-queue-length", type=int, default=1)
    parser.add_argument("--polling-period", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=Path("data/output/phase7/salad"))
    args = parser.parse_args()
    if args.cpu < 1 or args.memory_mb < 1024:
        parser.error("--cpu must be >= 1 and --memory-mb must be >= 1024")
    if not 0 <= args.min_replicas <= args.max_replicas <= 500:
        parser.error("replica limits must satisfy 0 <= min <= max <= 500")
    if not args.min_replicas <= args.replicas <= args.max_replicas:
        parser.error("--replicas must be between --min-replicas and --max-replicas")
    return args


def main() -> None:
    args = parse_args()
    secrets = _required_environment()

    queue = {
        "name": args.queue_name,
        "display_name": "AI Video Factory Jobs",
        "description": "Idempotent GPU jobs for AI Video Factory",
    }
    container_group = {
        "autostart_policy": True,
        "name": args.container_name,
        "display_name": args.display_name,
        "replicas": args.replicas,
        "restart_policy": "always",
        "container": {
            "image": args.image,
            "resources": {
                "cpu": args.cpu,
                "memory": args.memory_mb,
                "gpu_classes": args.gpu_class,
            },
            "environment_variables": {
                **secrets,
                "GPU_WORKER_MODE": "production",
                "GPU_WORKER_LEASE_SECONDS": os.getenv("GPU_WORKER_LEASE_SECONDS", "90"),
                "GPU_WORKER_HEARTBEAT_SECONDS": os.getenv("GPU_WORKER_HEARTBEAT_SECONDS", "30"),
                "SALAD_LOG_LEVEL": os.getenv("SALAD_LOG_LEVEL", "info"),
                "SALAD_QUEUE_ENABLED": "true",
            },
            "image_caching": True,
            "priority": os.getenv("SALAD_PRIORITY", "batch"),
        },
        "readiness_probe": {
            "http": {"path": "/ready", "port": 8080, "scheme": "http"},
            "initial_delay_seconds": 5,
            "period_seconds": 10,
            "failure_threshold": 6,
            "success_threshold": 1,
            "timeout_seconds": 5,
        },
        "queue_connection": {
            "path": "/jobs",
            "port": 8080,
            "queue_name": args.queue_name,
        },
        "queue_autoscaler": {
            "min_replicas": args.min_replicas,
            "max_replicas": args.max_replicas,
            "desired_queue_length": args.desired_queue_length,
            "polling_period": args.polling_period,
            "max_upscale_per_minute": max(1, args.max_replicas),
            "max_downscale_per_minute": max(1, args.max_replicas),
        },
    }

    queue_path = args.output_dir / "queue.json"
    container_path = args.output_dir / "container-group.json"
    _write_json(queue_path, queue)
    _write_json(container_path, container_group, secret=True)
    print(queue_path)
    print(container_path)
    print("WARNING: container-group.json contains secrets; do not commit or share it.")


if __name__ == "__main__":
    main()
