"""Controller-only provisioning of immutable, version-isolated LTX Salad groups.

The regular run command only records routes and queues jobs. All Salad
mutations are performed by the singleton controller while it owns leadership.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .salad import DEFAULT_SALAD_API_BASE_URL, SaladClient, SaladDispatchConfig, SaladRequestError
from .worker_image_routes import WorkerImageRoute


class WorkerVersionProvisionError(RuntimeError):
    pass


def _probe(config: dict[str, Any], port: int) -> dict[str, Any]:
    return {
        "http": {"headers": [], "path": config["path"], "port": port, "scheme": "http"},
        "initial_delay_seconds": 0,
        "period_seconds": int(config["period_seconds"]),
        "failure_threshold": int(config["failure_threshold"]),
        "success_threshold": 1,
        "timeout_seconds": int(config["timeout_seconds"]),
    }


class WorkerVersionProvisioner:
    def __init__(
        self, *,
        document: dict[str, Any],
        api_key: str,
        organization: str,
        project: str,
        api_base_url: str = DEFAULT_SALAD_API_BASE_URL,
    ) -> None:
        self.document = document
        self.api_key = api_key
        self.organization = organization
        self.project = project
        self.api_base_url = api_base_url
        self.gpu_classes: list[dict[str, object]] | None = None

    def client(self, route: WorkerImageRoute) -> SaladClient:
        return SaladClient(SaladDispatchConfig(
            api_key=self.api_key,
            api_base_url=self.api_base_url,
            organization_name=self.organization,
            project_name=self.project,
            container_group_name=route.group_name,
        ))

    def _new_group(self, route: WorkerImageRoute, client: SaladClient) -> None:
        service = self.document["services"][route.service]
        stack = self.document["stack"]
        if self.gpu_classes is None:
            self.gpu_classes = client.list_gpu_classes()
        gpu_names = service["resources"]["gpu_class_names"]
        gpu_ids: list[str] = []
        for name in gpu_names:
            matches = [
                str(item.get("id")) for item in self.gpu_classes
                if item.get("name") == name and item.get("id")
            ]
            if len(matches) != 1:
                raise WorkerVersionProvisionError(
                    f"Reviewed GPU class {name!r} did not resolve uniquely in Salad"
                )
            gpu_ids.extend(matches)
        env: dict[str, str] = {
            **{str(k): str(v) for k, v in stack["shared_environment"].items()},
            **{str(k): str(v) for k, v in service["environment"].items()},
        }
        required = set(stack.get("shared_required_environment", []))
        required.update(service.get("required_environment", []))
        for key in required:
            secret = str(os.getenv(str(key)) or "").strip()
            if not secret:
                raise WorkerVersionProvisionError(
                    f"Cannot provision isolated LTX worker: missing {key}"
                )
            env[str(key)] = secret
        env["INFERENCE_WORKER_IMAGE"] = route.image
        port = int(stack["container_port"])
        if port != 8080:
            raise WorkerVersionProvisionError("Reviewed Salad LTX worker port has changed")
        resources = service["resources"]
        payload: dict[str, Any] = {
            "name": route.group_name,
            "display_name": route.service + " " + route.image.rsplit(":", 1)[-1][:16],
            "autostart_policy": False,
            "replicas": int(service["capacity"]["start_replicas"]),
            "restart_policy": stack["restart_policy"],
            "scheduled_scaling_enabled": False,
            "container": {
                "image": route.image,
                "resources": {
                    "cpu": int(resources["cpu"]),
                    "memory": int(resources["memory"]),
                    "gpu_classes": gpu_ids,
                    "shm_size": int(resources["shm_size"]),
                    "storage_amount": int(resources["storage_amount"]),
                },
                "environment_variables": env,
                "image_caching": True,
                "priority": service["priority"],
            },
            "startup_probe": _probe(service["probes"]["startup"], port),
            "readiness_probe": _probe(service["probes"]["readiness"], port),
            "liveness_probe": _probe(stack["shared_liveness_probe"], port),
        }
        client.create_container_group(payload)

    def ensure(self, route: WorkerImageRoute, *, create: bool) -> bool:
        """Return True if present; register no mutation if no work requires it."""
        client = self.client(route)
        try:
            group = client.describe_container_group()
        except SaladRequestError as exc:
            if exc.status_code != 404:
                raise
            if not create:
                return False
            # This method is only invoked by the controller under leadership.
            self._new_group(route, client)
            deadline = time.monotonic() + 120
            while True:
                try:
                    group = client.describe_container_group()
                    break
                except SaladRequestError as error:
                    if error.status_code != 404 or time.monotonic() >= deadline:
                        raise
                    time.sleep(5)
        container = group.get("container")
        actual = container.get("image") if isinstance(container, dict) else None
        if actual != route.image:
            raise WorkerVersionProvisionError(
                f"Isolated group {route.group_name} has unexpected Docker image "
                f"{actual!r}; refusing to modify or route any jobs to it"
            )
        return True
