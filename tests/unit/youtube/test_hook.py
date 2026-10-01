"""The automatic hook is strictly after verified local publication, not generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from ai_video_factory.config import Settings
from ai_video_factory.integrations.youtube.errors import YouTubeValidationError
from ai_video_factory.integrations.youtube.hook import publish_verified_result
from ai_video_factory.integrations.youtube.models import YouTubePublishResult
from ai_video_factory.integrations.youtube.publisher import publish_to_youtube
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
    failure = json.loads((output / "youtube_publish_error.json").read_text())
    assert failure["schema_version"] == "youtube-publish-trigger/v1"
    assert failure["status"] == "failed_safe"
    assert "refresh_token" not in str(failure)


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


def test_real_stateful_publisher_is_exercised_from_verified_managed_result(
    tmp_path: Path, monkeypatch,
) -> None:
    output, published, config = fixture_result(tmp_path)
    from ai_video_factory.integrations.youtube import hook as youtube_hook

    thumbnail_path = published / "thumbnail.png"
    Image.new("RGB", (160, 90)).save(thumbnail_path)
    metadata = published / "youtube.json"
    document = json.loads(metadata.read_text())
    document["thumbnail_path"] = "thumbnail.png"
    metadata.write_text(json.dumps(document))
    original_manifest = (published / "manifest.json").read_bytes()
    events = []

    def video_upload(youtube, request, **kwargs):
        kwargs["on_started"]()
        events.append("videos.insert")
        kwargs["on_completed"]("hook_video_ABC")
        return YouTubePublishResult(
            video_id="hook_video_ABC",
            url="https://www.youtube.com/watch?v=hook_video_ABC",
            privacy=request.privacy, channel=request.channel,
            thumbnail_uploaded=False,
        )

    def thumb_upload(youtube, video_id, image):
        snapshot = json.loads((output / "youtube_publish.json").read_text())
        assert snapshot["video_id"] == video_id
        assert snapshot["status"] == "uploaded"
        events.append("thumbnails.set")

    def injected_publisher(request, **kwargs):
        return publish_to_youtube(
            request, **kwargs, youtube=object(), upload_fn=video_upload,
            thumbnail_fn=thumb_upload,
        )

    monkeypatch.setattr(youtube_hook, "publish_to_youtube", injected_publisher)
    first = publish_verified_result(
        output=output, published=published,
        script_name="guion", managed_target=published, config=config,
    )
    assert first["status"] == "completed"
    assert first["thumbnail_uploaded"] is True
    assert events == ["videos.insert", "thumbnails.set"]
    assert (published / "manifest.json").read_bytes() == original_manifest
    assert json.loads((published / "youtube_publish.json").read_text())["status"] == (
        "completed"
    )
    second = publish_verified_result(
        output=output, published=published,
        script_name="guion", managed_target=published, config=config,
    )
    assert second == first
    assert events == ["videos.insert", "thumbnails.set"]
    Image.new("RGB", (120, 60)).save(thumbnail_path)
    third = publish_verified_result(
        output=output, published=published,
        script_name="guion", managed_target=published, config=config,
    )
    assert third["video_id"] == first["video_id"]
    assert events == ["videos.insert", "thumbnails.set", "thumbnails.set"]


def test_explicit_youtube_only_uses_saved_result_without_any_gpu_jobs(
    tmp_path: Path, monkeypatch,
) -> None:
    output, published, config = fixture_result(tmp_path)
    script = tmp_path / "guion.txt"
    script.write_text("script")
    monkeypatch.setattr(pipeline, "settings", config)
    monkeypatch.setattr(pipeline, "parse_args", lambda: SimpleNamespace(
        script_file=script,
        scripts_dir=tmp_path / "scripts",
        avatars_dir=tmp_path / "avatars",
        list_scripts=False,
        list_avatars=False,
        qr_only=False,
        script=None,
        youtube_only=True,
        output=output.parent,
        managed_target=published,
        results_root=tmp_path / "results",
    ))
    monkeypatch.setattr(pipeline, "select_script", lambda **kwargs: script)
    hook = Mock(return_value=published)
    monkeypatch.setattr(pipeline, "_publish_if_ready", hook)
    asyncio.run(pipeline.main())
    hook.assert_called_once()
    assert hook.call_args.kwargs["fail_on_youtube_error"] is True
    assert hook.call_args.args[0] == output


def test_explicit_distribution_error_sets_nonzero_failure_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    output, published, config = fixture_result(tmp_path)
    (output / "run_report.json").write_text(json.dumps({
        "run": {"video_generation": {"status": "completed"}},
    }))
    script = tmp_path / "guion.txt"
    script.write_text("script")
    monkeypatch.setattr(pipeline, "settings", config)
    monkeypatch.setattr(
        pipeline, "publish_managed_run", Mock(return_value=published),
    )
    def denied(**kwargs):
        raise YouTubeValidationError("Metadata denied")

    monkeypatch.setattr(
        "ai_video_factory.integrations.youtube.hook.publish_verified_result", denied,
    )
    with pytest.raises(RuntimeError, match="verified local MP4 is unchanged"):
        pipeline._publish_if_ready(
            output, script, tmp_path / "results", published,
            fail_on_youtube_error=True,
        )
    assert (output / "youtube_publish_error.json").is_file()
    assert (published / "guion.mp4").is_file()
