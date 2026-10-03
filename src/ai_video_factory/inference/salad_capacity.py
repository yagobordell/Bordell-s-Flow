from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ai_video_factory.workers.breeze_tts2.model import BREEZE_TTS2_TASK
from ai_video_factory.workers.fish_speech.model import FISH_SPEECH_TASK
from ai_video_factory.workers.ideogram4.model import (
    IDEOGRAM4_KEYFRAME_TASK,
    IDEOGRAM4_REFERENCE_TASK,
)
from ai_video_factory.workers.ltx25.a2v import LTX_A2V_TASK
from ai_video_factory.workers.ltx25.model import LTX_VIDEO_TASK
from ai_video_factory.workers.qwen_image_21.model import (
    QWEN_IMAGE_21_KEYFRAME_TASK,
    QWEN_IMAGE_21_REFERENCE_TASK,
)
from ai_video_factory.workers.realesrgan.model import REALESRGAN_TASK
from ai_video_factory.workers.whisper.model import WHISPER_TRANSCRIPTION_TASK

from .salad import DEFAULT_SALAD_API_BASE_URL, SaladClient, SaladDispatchConfig
from .salad_autoscaler import (
    ACTIVE_STATUSES,
    AutoscalerServiceBinding,
    PostgresAutoscalerStore,
    PredictiveSaladAutoscaler,
    load_predictive_autoscaler_config,
)
from .salad_version_provision import WorkerVersionProvisioner
from .worker_image_routes import list_routes


@dataclass(frozen=True, slots=True)
class SaladCapacityRuntime:
    autoscaler: PredictiveSaladAutoscaler
    store: PostgresAutoscalerStore

    def close(self) -> None:
        self.store.close()


_SERVICE_TASKS: dict[str, tuple[str, ...]] = {
    "whisper": (WHISPER_TRANSCRIPTION_TASK,),
    "breeze_tts2": (BREEZE_TTS2_TASK,),
    "fish_speech": (FISH_SPEECH_TASK,),
    "ideogram4": (IDEOGRAM4_REFERENCE_TASK, IDEOGRAM4_KEYFRAME_TASK),
    "qwen_image_21": (QWEN_IMAGE_21_REFERENCE_TASK, QWEN_IMAGE_21_KEYFRAME_TASK),
    "ltx25_i2v": (LTX_VIDEO_TASK,),
    "ltx25": (LTX_A2V_TASK,),
    "realesrgan": (REALESRGAN_TASK,),
}

# Media Pipeline uses 60s for image work, 206s for LTX and 53s for clip
# upscaling. Bordell starts the remaining workloads at the same conservative 60s
# fallback and replaces it with the configured runtime percentile as history accrues.
_FALLBACK_RUNTIME_SECONDS: dict[str, float] = {
    "whisper": 60.0,
    "breeze_tts2": 60.0,
    "fish_speech": 60.0,
    "ideogram4": 60.0,
    "qwen_image_21": 60.0,
    "ltx25_i2v": 206.0,
    "ltx25": 206.0,
    "realesrgan": 53.0,
}


def build_salad_capacity_runtime(
    *,
    services_path: Path,
    postgres_dsn: str,
    salad_api_key: str,
    logger=print,
) -> SaladCapacityRuntime:
    document = _load_services_document(services_path)
    stack = _required_object(document, "stack")
    services = _required_object(document, "services")
    organization = _required_salad_account_env("SALAD_ORGANIZATION")
    project = _required_salad_account_env("SALAD_PROJECT")
    service_order = stack.get("service_order")
    if not isinstance(service_order, list) or not service_order:
        raise RuntimeError(
            "deploy/salad/services.json stack.service_order must be a non-empty list"
        )
    normalized_service_order = [str(item).strip() for item in service_order]
    if any(not item for item in normalized_service_order):
        raise RuntimeError("stack.service_order cannot contain blank service names")
    if len(normalized_service_order) != len(set(normalized_service_order)):
        raise RuntimeError("stack.service_order must contain unique service names")

    bindings: dict[str, AutoscalerServiceBinding] = {}
    clients: dict[str, SaladClient] = {}
    group_owners: dict[str, str] = {}
    api_base_url = os.getenv("SALAD_API_BASE_URL", DEFAULT_SALAD_API_BASE_URL).strip()

    for service_name in normalized_service_order:
        if service_name not in _SERVICE_TASKS:
            raise RuntimeError(
                f"Salad service {service_name!r} is missing from the predictive capacity catalog"
            )
        raw_service = services.get(service_name)
        if not isinstance(raw_service, dict):
            raise RuntimeError(f"Salad service configuration is missing: {service_name}")
        capacity = _required_object(raw_service, "capacity")
        max_replicas = int(capacity.get("max_replicas") or 0)
        if max_replicas < 1:
            raise RuntimeError(f"Salad service {service_name} max_replicas must be >= 1")
        group_name = _required_text(raw_service, "group_name")
        previous_owner = group_owners.get(group_name)
        if previous_owner is not None:
            raise RuntimeError(
                "Salad group_name must be unique across services: "
                f"{group_name!r} is assigned to {previous_owner!r} and {service_name!r}"
            )
        group_owners[group_name] = service_name
        binding = AutoscalerServiceBinding(
            workload=service_name,
            task_names=_SERVICE_TASKS[service_name],
            autoscaler_env_prefix=service_name.upper(),
            fallback_runtime_seconds=_FALLBACK_RUNTIME_SECONDS[service_name],
            max_replicas=max_replicas,
        )
        bindings[service_name] = binding
        clients[service_name] = SaladClient(
            SaladDispatchConfig(
                api_key=salad_api_key,
                api_base_url=api_base_url,
                organization_name=organization,
                project_name=project,
                container_group_name=group_name,
            )
        )

    config = load_predictive_autoscaler_config(stage_bindings=tuple(bindings.values()))
    total_stage_capacity = sum(config.stage_max_replicas.values())
    if config.project_max_replicas >= total_stage_capacity:
        logger(
            "Warning: SALAD_AUTOSCALER_PROJECT_MAX_REPLICAS="
            f"{config.project_max_replicas} does not constrain the configured "
            f"per-service maximum of {total_stage_capacity} replicas."
        )
    store = PostgresAutoscalerStore(postgres_dsn)
    autoscaler = PredictiveSaladAutoscaler(
        config=config,
        store=store,
        clients=clients,
        bindings=bindings,
        managed_group_names=set(group_owners),
        logger=logger,
    )
    provisioner = WorkerVersionProvisioner(
        document=document, api_key=salad_api_key, organization=organization,
        project=project, api_base_url=api_base_url,
    )
    static_group_names = set(group_owners)

    def refresh_version_routes() -> None:
        # This runs only inside the session-leader reconciliation loop.
        # Static workers continue handling pre-migration jobs with no image key.
        routes = list_routes(postgres_dsn)
        max_routes = int(os.getenv("SALAD_VERSION_MAX_GROUPS", "16"))
        if len(routes) > max_routes:
            raise RuntimeError(
                f"Registered LTX Docker versions ({len(routes)}) exceed "
                f"SALAD_VERSION_MAX_GROUPS={max_routes}; archive idle routes first"
            )
        registered_names = {route.group_name for route in routes}
        for route in routes:
            service = route.service
            dynamic = route.stage
            if dynamic in static_group_names:
                raise RuntimeError("Dynamic LTX route conflicts with static service name")
            definition = services[service]
            if not isinstance(definition, dict):
                raise RuntimeError("LTX route refers to missing reviewed service")
            capacity = definition["capacity"]
            binding = AutoscalerServiceBinding(
                workload=dynamic,
                task_names=(route.task_name,),
                autoscaler_env_prefix=service.upper(),
                fallback_runtime_seconds=_FALLBACK_RUNTIME_SECONDS[service],
                max_replicas=int(capacity["max_replicas"]),
                required_worker_image=route.image,
            )
            # Creating a registered group requires actual unfinished demand.
            # A mere stale registration never allocates a new GPU group.
            if route.routing_contract != "v1":
                # Never consume a GPU for an older/unverified Docker that
                # cannot poll the new image-specific task aliases.
                continue
            demand = bool(store.list_stage_jobs(
                binding=binding, statuses=ACTIVE_STATUSES,
            ))
            exists = provisioner.ensure(route, create=demand)
            if not exists:
                continue
            if dynamic not in bindings:
                bindings[dynamic] = binding
                clients[dynamic] = provisioner.client(route)
        autoscaler.managed_group_names = frozenset(
            static_group_names | registered_names
        )
        # The config is immutable; rebuild when a new stage first appears.
        if set(autoscaler.clients) != set(clients):
            autoscaler.clients = dict(clients)
            autoscaler.bindings = dict(bindings)
            autoscaler.config = load_predictive_autoscaler_config(
                stage_bindings=tuple(bindings.values()),
            )

    autoscaler.route_refresher = refresh_version_routes
    return SaladCapacityRuntime(autoscaler=autoscaler, store=store)


def _required_salad_account_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Missing {name} in the environment; refusing to select a Salad account"
        )
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) is None:
        raise RuntimeError(
            f"{name} must be a Salad organization/project slug "
            "(letters, digits, hyphens or underscores)"
        )
    return value


def _load_services_document(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"Salad services manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("deploy/salad/services.json must contain a JSON object")
    return payload


def _required_object(parent: dict[str, object], name: str) -> dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise RuntimeError(f"Salad configuration requires object {name!r}")
    return value


def _required_text(parent: dict[str, object], name: str) -> str:
    value = str(parent.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Salad configuration requires non-empty {name!r}")
    return value
