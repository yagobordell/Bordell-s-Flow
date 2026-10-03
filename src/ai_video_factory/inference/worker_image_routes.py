"""Immutable image-specific LTX routes shared by all pipeline clients and the controller.

Registration creates *demand metadata*, never mutates a running Salad group.
Only the singleton Capacity Controller provisions/routes new versions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

REPOSITORY = "docker.io/yagobordell/ai-video-factory"
_IMAGE = re.compile(r"^docker[.]io/yagobordell/ai-video-factory@sha256:([0-9a-f]{64})$")
Service = Literal["ltx25", "ltx25_i2v"]


@dataclass(frozen=True)
class WorkerImageRoute:
    service: Service
    image: str
    group_name: str
    routing_contract: str = "unverified"

    @property
    def stage(self) -> str:
        return self.service + "_" + self.image.rsplit(":", 1)[-1][:16]

    @property
    def task_name(self) -> str:
        # Crucial migration safety: an unpatched legacy worker only polls the
        # *base* task and therefore cannot steal any image-pinned work.
        base = (
            "video.ltx25.audio_to_video" if self.service == "ltx25"
            else "video.ltx25.generate"
        )
        return base + ".v" + self.image.rsplit(":", 1)[-1][:16]


def route_for_image(service: Service, image: str) -> WorkerImageRoute:
    if service not in {"ltx25", "ltx25_i2v"}:
        raise ValueError("Only isolated LTX A2V and I2V can register worker versions")
    match = _IMAGE.fullmatch(image)
    if match is None:
        raise ValueError(
            "LTX worker image must be a complete immutable digest from "
            "docker.io/yagobordell/ai-video-factory"
        )
    digest = match[1]
    return WorkerImageRoute(
        service=service,
        image=image,
        group_name="ai-video-factory-" + service.replace("_", "-") + "-" + digest[:16],
    )


def register_route(dsn: str, *, service: Service, image: str) -> WorkerImageRoute:
    """Idempotent, concurrency-safe registration. No Salad deployment from run."""
    import psycopg

    route = route_for_image(service, image)
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        row = connection.execute(
            """
            INSERT INTO gpu.worker_image_routes (service, image, group_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (service, image) DO UPDATE
            SET image = EXCLUDED.image
            RETURNING group_name, routing_contract
            """,
            (route.service, route.image, route.group_name),
        ).fetchone()
    if row is None or row[0] != route.group_name:
        raise RuntimeError("LTX image route registration conflicted with its group identity")
    return replace(route, routing_contract=str(row[1]))


def approve_route(dsn: str, *, service: Service, image: str) -> WorkerImageRoute:
    """Operator attests the Docker digest was built with routing contract v1."""
    import psycopg

    route = register_route(dsn, service=service, image=image)
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        row = connection.execute(
            """
            UPDATE gpu.worker_image_routes
            SET routing_contract = 'v1'
            WHERE service = %s AND image = %s AND group_name = %s
            RETURNING routing_contract
            """,
            (route.service, route.image, route.group_name),
        ).fetchone()
    if row is None or row[0] != "v1":
        raise RuntimeError("LTX routing compatibility approval was not persisted")
    return replace(route, routing_contract="v1")


def list_routes(dsn: str) -> list[WorkerImageRoute]:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as connection:
        rows = connection.execute(
            "SELECT service, image, group_name, routing_contract FROM gpu.worker_image_routes "
            "ORDER BY registered_at ASC, service, image"
        ).fetchall()
    routes = [
        replace(route_for_image(row[0], row[1]), routing_contract=str(row[3]))
        for row in rows
    ]
    if any(route.group_name != row[2] for route, row in zip(routes, rows, strict=True)):
        raise RuntimeError("Shared LTX route registry contains a forged group name")
    return routes
