from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import preflight_video_factory as preflight


def _services() -> dict:
    return {
        "stack": {
            "organization": "org",
            "project": "project",
        },
        "services": {
            "whisper": {"queue_name": "whisper-q"},
            "ltx25": {"queue_name": "ltx-q"},
        },
    }


def test_salad_queue_preflight_allows_only_manifest_owned_ltx_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "phase8" / "video_generation_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "transport_job_id": "transport-owned",
                        "transport_status": "running",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")

    def fake_jobs(*, base_url: str, queue_name: str, api_key: str):
        assert base_url.endswith("/organizations/org/projects/project")
        assert api_key == "test-key"
        if queue_name == "ltx-q":
            return [{"id": "transport-owned", "status": "running"}]
        return []

    monkeypatch.setattr(preflight, "_queue_jobs", fake_jobs)

    result = preflight._check_salad_queues(_services(), tmp_path)

    assert result["ltx25"] == {
        "active_jobs": 1,
        "recognized_resume_jobs": 1,
    }
    assert result["whisper"]["active_jobs"] == 0


def test_salad_queue_preflight_rejects_unowned_active_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.settings, "salad_api_key", "test-key")
    monkeypatch.setattr(
        preflight,
        "_queue_jobs",
        lambda **_: [{"id": "foreign-job", "status": "pending"}],
    )

    with pytest.raises(RuntimeError, match="not owned by the local resume manifest"):
        preflight._check_salad_queues(_services(), tmp_path)


def test_resume_transport_reader_rejects_malformed_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "phase8" / "video_generation_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"jobs": "not-a-list"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid jobs"):
        preflight._active_resume_transport_ids(tmp_path)
