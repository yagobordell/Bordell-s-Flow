from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .preflight_video_factory import _check_salad_queues, _load_services
except ImportError:  # Direct execution: python scripts/check_salad_queue_ready.py
    from preflight_video_factory import _check_salad_queues, _load_services


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hard pre-GPU Salad queue ownership/state guard for one service."
    )
    parser.add_argument("service")
    parser.add_argument(
        "--services",
        type=Path,
        default=Path("deploy/salad/services.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/output"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    document = _load_services(args.services)
    if args.service not in document["services"]:
        raise SystemExit(f"Unknown Salad service: {args.service}")
    try:
        result = _check_salad_queues(
            document,
            args.output_dir,
            service_names={args.service},
            allow_transient_defer=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"SALAD_QUEUE_PRE_GPU_GUARD_FAILED: {exc}") from exc

    print(
        "SALAD_QUEUE_PRE_GPU_GUARD_OK "
        f"service={args.service} state={json.dumps(result[args.service], sort_keys=True)}"
    )


if __name__ == "__main__":
    main()
