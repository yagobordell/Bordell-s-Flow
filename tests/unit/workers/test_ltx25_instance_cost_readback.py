from __future__ import annotations

from typing import Any

import pytest

from ai_video_factory.inference.salad import SaladClient, SaladDispatchConfig


def _client() -> SaladClient:
    return SaladClient(
        SaladDispatchConfig(
            api_key="unused-for-offline-test",
            api_base_url="https://example.invalid/api/public",
            organization_name="org",
            project_name="project",
            container_group_name="group",
        )
    )


def test_get_container_group_instance_reads_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    observed: list[str] = []

    def fake_get(url: str) -> dict[str, Any]:
        observed.append(url)
        return {"id": "winner", "version": 27, "deletion_cost": 100000}

    monkeypatch.setattr(client, "_get", fake_get)
    result = client.get_container_group_instance("winner")
    assert result["deletion_cost"] == 100000
    assert observed == [
        "https://example.invalid/api/public/organizations/org/projects/project"
        "/containers/group/instances/winner"
    ]


def test_get_container_group_instance_rejects_wrong_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(client, "_get", lambda _: {"id": "someone-else"})
    with pytest.raises(RuntimeError, match="different instance"):
        client.get_container_group_instance("winner")
    with pytest.raises(ValueError, match="instance_id is required"):
        client.get_container_group_instance(" ")
