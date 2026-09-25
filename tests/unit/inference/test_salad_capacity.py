import json
from pathlib import Path

import pytest

from ai_video_factory.inference.salad_capacity import build_salad_capacity_runtime


def _write_manifest(
    path: Path,
    *,
    service_order: list[str],
    groups: dict[str, str],
) -> Path:
    document = {
        "stack": {
            "organization": "test-org",
            "project": "test-project",
            "service_order": service_order,
        },
        "services": {
            service: {
                "group_name": group_name,
                "capacity": {"max_replicas": 1},
            }
            for service, group_name in groups.items()
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_capacity_runtime_rejects_duplicate_service_order_before_postgres(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(
        tmp_path / "services.json",
        service_order=["ltx25", "ltx25"],
        groups={"ltx25": "ltx-group"},
    )

    with pytest.raises(RuntimeError, match="service_order must contain unique"):
        build_salad_capacity_runtime(
            services_path=manifest,
            postgres_dsn="postgresql://invalid/not-used",
            salad_api_key="test-key",
        )


def test_capacity_runtime_rejects_duplicate_remote_group_identity_before_postgres(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(
        tmp_path / "services.json",
        service_order=["ltx25", "whisper"],
        groups={
            "ltx25": "shared-group",
            "whisper": "shared-group",
        },
    )

    with pytest.raises(RuntimeError, match="group_name must be unique"):
        build_salad_capacity_runtime(
            services_path=manifest,
            postgres_dsn="postgresql://invalid/not-used",
            salad_api_key="test-key",
        )
