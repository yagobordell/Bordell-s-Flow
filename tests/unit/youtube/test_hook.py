"""The automatic hook is strictly after verified local publication, not generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_video_factory.config import Settings
from ai_video_factory.integrations.youtube.errors import YouTubeValidationError
from ai_video_factory.integrations.youtube.hook import publish_verified_result
from ai_video_factory.integrations.youtube.models import YouTubePublishResult
from scripts.pipeline import run_b_pipeline as pipeline


def fixture_result(tmp_path: Path, *, managed: bool = True):
    output = tmp_path / "work" / "guion"
    published = tmp_path / "data" / "Jorge" / "guion"
    output.mkdir(parents=True)
    published.mkdir(parents=True)
    video = published / "guion.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 128)
    marker = {
        "schema_version": (
            "managed-b-pipeline-v1" if managed else "b-pipeline-publication-v1"
        ),
        "files": {"guion.mp4": hashlib.sha256(video.read_bytes()).hexdigest()},
    }
    (published / "manifest.json").write_text(json.dumps(marker))
    (published / "youtube.json").write_text(json.dumps({
        "schema_version": "youtube-publish/v1",
        "channel": "test01",
        "title": "Managed verified video",
        "privacy": "private",
    }))
    config = Settings(
        _env_file=None, youtube_enabled=True,
        youtube_keystore_dir=tmp_path / "external-keystore",
        youtube_state_dir=tmp_path / "publication-registry",
    )
    return output, published, config


def test_verified_managed_result_resolves_visible_metadata(tmp_path: Path, monkeypatch):
    output, published, config = fixture_result(tmp_path)
    called = []
    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.YouTubeStore.ensure_directory",
        lambda obj: obj.directory,
    )

    def fake_publish(request, **kwargs):
        called.append((request, kwargs))
        return YouTubePublishResult(
            video_id="vidABC", url="https://www.youtube.com/watch?v=vidABC",
            channel=request.channel, privacy=request.privacy, thumbnail_uploaded=False,
        )

    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_to_youtube", fake_publish
    )
    result = publish_verified_result(
        output=output, published=published, script_name="guion",
        managed_target=published, config=config,
    )
    assert result["url"] == "https://www.youtube.com/watch?v=vidABC"
    assert called[0][0].video_path == published / "guion.mp4"
    assert called[0][0].title == "Managed verified video"
    assert called[0][1]["state_path"] == output / "youtube_publish.json"
    assert called[0][1]["state_dir"] == config.youtube_state_dir


def test_bad_video_hash_and_missing_metadata_never_publish(tmp_path: Path, monkeypatch):
    output, published, config = fixture_result(tmp_path)
    fake = Mock(side_effect=AssertionError("Must not contact YouTube"))
    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_to_youtube", fake
    )
    (published / "guion.mp4").write_bytes(b"changed")
    with pytest.raises(YouTubeValidationError, match="differs"):
        publish_verified_result(
            output=output, published=published, script_name="guion",
            managed_target=published, config=config,
        )
    fake.assert_not_called()
    output, published, config = fixture_result(tmp_path / "new")
    (published / "youtube.json").unlink()
    with pytest.raises(YouTubeValidationError, match="no youtube.json"):
        publish_verified_result(
            output=output, published=published, script_name="guion",
            managed_target=published, config=config,
        )
    fake.assert_not_called()


def test_disabled_hook_never_reads_paths_or_credentials(tmp_path: Path):
    disabled = Settings(_env_file=None, youtube_enabled=False)
    assert publish_verified_result(
        output=tmp_path / "missing",
        published=tmp_path / "missing",
        script_name="guion", config=disabled,
    ) == {"status": "disabled"}


def test_b_pipeline_hook_only_on_finished_local_result(tmp_path: Path, monkeypatch):
    output, published, config = fixture_result(tmp_path)
    script_file = tmp_path / "guion.txt"
    script_file.write_text("original text")
    (output / "run_report.json").write_text(json.dumps({
        "run": {"video_generation": {"status": "completed"}},
    }))
    monkeypatch.setattr(pipeline, "settings", config)
    local = Mock(return_value=published)
    hook = Mock(return_value={
        "status": "completed", "url": "https://youtube.com/watch?v=vidABC",
    })
    monkeypatch.setattr(pipeline, "publish_managed_run", local)
    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_verified_result", hook
    )
    assert pipeline._publish_if_ready(
        output, script_file, tmp_path / "results", published
    ) == published
    local.assert_called_once()
    hook.assert_called_once()
    hook_args = hook.call_args.kwargs
    assert hook_args["published"] == published
    assert hook_args["script_name"] == "guion"

    local.reset_mock()
    hook.reset_mock()
    (output / "run_report.json").write_text(json.dumps({
        "run": {"video_generation": {"status": "pending"}},
    }))
    assert pipeline._publish_if_ready(
        output, script_file, tmp_path / "results", published
    ) is None
    local.assert_not_called()
    hook.assert_not_called()


def test_distribution_failure_does_not_change_generation_outcome(
    tmp_path: Path, monkeypatch, capsys,
):
    output, published, config = fixture_result(tmp_path)
    script = tmp_path / "guion.txt"
    script.write_text("script")
    (output / "run_report.json").write_text(json.dumps({
        "run": {"video_generation": {"status": "completed"}},
    }))
    monkeypatch.setattr(pipeline, "settings", config)
    local = Mock(return_value=published)
    monkeypatch.setattr(pipeline, "publish_managed_run", local)

    def broken(**kwargs):
        raise YouTubeValidationError("Metadata missing")

    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_verified_result", broken
    )
    assert pipeline._publish_if_ready(
        output, script, tmp_path / "results", published
    ) == published
    assert local.call_count == 1
    output_text = capsys.readouterr().out
    assert "failed separately" in output_text
    assert "Metadata missing" in output_text


def test_disabled_pipeline_preserves_existing_behavior(tmp_path: Path, monkeypatch):
    output, published, config = fixture_result(tmp_path)
    config.youtube_enabled = False
    script = tmp_path / "guion.txt"
    script.write_text("script")
    (output / "run_report.json").write_text(json.dumps({
        "run": {"video_generation": {"status": "completed"}},
    }))
    monkeypatch.setattr(pipeline, "settings", config)
    local = Mock(return_value=published)
    monkeypatch.setattr(pipeline, "publish_managed_run", local)
    hook = Mock(side_effect=AssertionError("Disabled must never contact YouTube"))
    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_verified_result", hook
    )
    assert pipeline._publish_if_ready(
        output, script, tmp_path / "results", published
    ) == published
    local.assert_called_once()
    hook.assert_not_called()
