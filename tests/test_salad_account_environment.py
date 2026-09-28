from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_video_factory.inference import salad_capacity


def _services_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(
            {
                "stack": {
                    # These old fields must never select the target account.
                    "organization": "wrong-organization",
                    "project": "wrong-project",
                    "service_order": ["ltx25"],
                },
                "services": {
                    "ltx25": {
                        "group_name": "test-ltx25",
                        "capacity": {"max_replicas": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _stub_remote_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened_stores: list[str] = []

    def store(dsn: str) -> SimpleNamespace:
        opened_stores.append(dsn)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(salad_capacity, "PostgresAutoscalerStore", store)
    monkeypatch.setattr(
        salad_capacity,
        "load_predictive_autoscaler_config",
        lambda *, stage_bindings: SimpleNamespace(
            stage_max_replicas={"ltx25": 1}, project_max_replicas=1
        ),
    )
    monkeypatch.setattr(
        salad_capacity,
        "PredictiveSaladAutoscaler",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    return opened_stores


def _build(tmp_path: Path) -> salad_capacity.SaladCapacityRuntime:
    return salad_capacity.build_salad_capacity_runtime(
        services_path=_services_manifest(tmp_path),
        postgres_dsn="postgresql://unused/test",
        salad_api_key="fake-api-key",
        logger=lambda _message: None,
    )


def test_capacity_controller_uses_account_environment_not_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALAD_ORGANIZATION", "teammate-org")
    monkeypatch.setenv("SALAD_PROJECT", "teammate-dev")
    opened_stores = _stub_remote_runtime(monkeypatch)

    runtime = _build(tmp_path)
    client = runtime.autoscaler.clients["ltx25"]

    assert client.config.organization_name == "teammate-org"
    assert client.config.project_name == "teammate-dev"
    assert client.config.container_group_name == "test-ltx25"
    assert opened_stores == ["postgresql://unused/test"]


@pytest.mark.parametrize("missing", ["SALAD_ORGANIZATION", "SALAD_PROJECT"])
def test_capacity_controller_fails_closed_without_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("SALAD_ORGANIZATION", "teammate-org")
    monkeypatch.setenv("SALAD_PROJECT", "teammate-dev")
    monkeypatch.delenv(missing)
    # Deprecated names must never act as a fallback to a different account.
    monkeypatch.setenv("SALAD_ORGANIZATION_NAME", "wrong-organization")
    monkeypatch.setenv("SALAD_PROJECT_NAME", "wrong-project")
    opened_stores = _stub_remote_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match=f"Missing {missing}"):
        _build(tmp_path)

    assert opened_stores == []


@pytest.mark.parametrize("invalid", ["other/project", "project?name", "has spaces"])
def test_capacity_controller_rejects_unsafe_account_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("SALAD_ORGANIZATION", invalid)
    monkeypatch.setenv("SALAD_PROJECT", "teammate-dev")
    opened_stores = _stub_remote_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="SALAD_ORGANIZATION must be"):
        _build(tmp_path)

    assert opened_stores == []


def test_deployment_manifest_does_not_pin_an_account() -> None:
    manifest = Path(__file__).resolve().parents[1] / "deploy" / "salad" / "services.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))

    assert "organization" not in document["stack"]
    assert "project" not in document["stack"]


def test_original_env_example_leaves_salad_values_blank_and_uses_postgres() -> None:
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    manifest = json.loads(
        (root / "deploy" / "salad" / "services.json").read_text(encoding="utf-8")
    )

    assert "SALAD_ORGANIZATION=" in example
    assert "SALAD_PROJECT=" in example
    assert "SALAD_ORGANIZATION_NAME=" not in example
    assert "SALAD_PROJECT_NAME=" not in example
    # The versioned example is intentionally blank, not tied to anyone's account.
    assert "SALAD_QUEUE_NAME=\n" in example
    assert "SALAD_LOG_LEVEL=\n" in example
    assert "SALAD_QUEUE_ENABLED=\n" in example
    assert "SALAD_API_KEY=\n" in example
    assert "FISH_SPEECH_REFERENCE_PROFILE=project-narrator-v1" in example
    assert "FISH_SPEECH_REFERENCE_AUDIO_SHA256=bc9f0cb0ba8675aadb3247f9c93de532de693189faf31e849b9af2ec69cc97ab" in example
    assert manifest["stack"]["job_transport"] == "postgres"
