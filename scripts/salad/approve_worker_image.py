"""Approve a Docker image verified to support immutable image-specific queue tasks.

Use only after building and smoke-testing a worker containing the routing-v1
adapter. Attestation is explicit; the script cannot prove an arbitrary old
Docker digest contains newer application code.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from ai_video_factory.inference.worker_image_routes import approve_route

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=("ltx25", "ltx25_i2v"))
    parser.add_argument("--image", required=True, help="Complete reviewed Docker @sha256 digest")
    parser.add_argument(
        "--attest-routing-v1", action="store_true",
        help="Explicit confirmation that this exact image was rebuilt and "
             "smoke-tested with the routing-v1 worker adapter",
    )
    args = parser.parse_args()
    if not args.attest_routing_v1:
        parser.error(
            "Do not approve an unpatched pre-migration image. Rebuild and "
            "smoke-test the routing-v1 worker, then pass --attest-routing-v1."
        )
    for name in ("config/app.env", "config/salad.env", ".env"):
        load_dotenv(ROOT / name, override=False)
    dsn = (
        os.getenv("SALAD_CAPACITY_CONTROLLER_POSTGRES_DSN")
        or os.getenv("POSTGRES_DSN")
    )
    if not dsn:
        parser.error("A session-capable shared controller PostgreSQL DSN is required")
    route = approve_route(dsn, service=args.service, image=args.image)
    print(
        f"Approved routing-v1 group={route.group_name} "
        f"service={route.service} image={route.image}",
        flush=True,
    )


if __name__ == "__main__":
    main()
