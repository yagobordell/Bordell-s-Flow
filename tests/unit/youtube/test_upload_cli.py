"""CLI dry-run and manual publication orchestration, all operations offline."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_video_factory.config import Settings
from ai_video_factory.integrations.youtube.auth import YOUTUBE_UPLOAD_SCOPE
from ai_video_factory.integrations.youtube.models import YouTubePublishResult
from ai_video_factory.integrations.youtube.store import YouTubeStore
from scripts.youtube import upload as cli


def setup(tmp_path: Path, monkeypatch, **metadata) -> tuple[Path, Path]:
    key_dir = tmp_path / "outside-keystore"
    store = YouTubeStore(key_dir)
    store.save_token("test01", {
        "client_id": "DUMMY_CLIENT_FOR_TESTS",
        "client_secret": "DUMMY_SECRET_FOR_TESTS",
        "refresh_token": "DUMMY_TOKEN_FOR_TESTS",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [YOUTUBE_UPLOAD_SCOPE],
    })
    monkeypatch.setattr(
        cli, "settings", Settings(
            _env_file=None,
            youtube_keystore_dir=key_dir,
            youtube_state_dir=tmp_path / "isolated-publication-state",
        )
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40)
    path = tmp_path / "youtube.json"
    data = {
        "schema_version": "youtube-publish/v1",
        "channel": "test01", "title": "Test", "privacy": "private",
    }
    data.update(metadata)
    path.write_text(json.dumps(data))
    return video, path


def argv(video: Path, metadata: Path, *extra: str) -> list[str]:
    return ["--video", str(video), "--metadata", str(metadata), *extra]


def test_dry_run_is_offline_and_does_not_write_attempt(tmp_path: Path, monkeypatch, capsys):
    video, metadata = setup(tmp_path, monkeypatch)
    no_network = Mock(side_effect=AssertionError("Never connect to Google"))
    monkeypatch.setattr(cli, "load_credentials", no_network)
    monkeypatch.setattr(cli, "build_youtube_client", no_network)
    monkeypatch.setattr(cli, "upload_video", no_network)
    assert cli.main(argv(video, metadata, "--dry-run")) == 0
    report = capsys.readouterr().out
    assert '"privacy": "private"' in report
    assert '"channel_alias_unverified": "test01"' in report
    assert "DUMMY_SECRET" not in report
    assert "Dry run successful" in report
    assert not list(tmp_path.glob("*.youtube_publish.json"))
    assert not (tmp_path / "youtube-publications").exists()
    no_network.assert_not_called()


def test_real_upload_writes_receipt_then_second_run_never_reuploads(
    tmp_path: Path, monkeypatch, capsys
):
    video, metadata = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_credentials", Mock(return_value=object()))
    monkeypatch.setattr(cli, "build_youtube_client", Mock(return_value=object()))

    def simulate(youtube, request, **kwargs):
        kwargs["on_started"]()
        kwargs["progress"](80)
        kwargs["on_completed"]("video_ABC_123")
        return YouTubePublishResult(
            video_id="video_ABC_123",
            url="https://www.youtube.com/watch?v=video_ABC_123",
            privacy=request.privacy,
            channel=request.channel,
            thumbnail_uploaded=False,
        )

    upload_mock = Mock(side_effect=simulate)
    monkeypatch.setattr(cli, "upload_video", upload_mock)
    cmd = argv(video, metadata, "--yes")
    assert cli.main(cmd) == 0
    assert upload_mock.call_count == 1
    record = json.loads(next(tmp_path.glob("*.youtube_publish.json")).read_text())
    assert record["video_id"] == "video_ABC_123"
    assert record["status"] == "completed"
    assert cli.main(cmd) == 0
    assert upload_mock.call_count == 1
    assert "YouTube publication complete or safely reused" in capsys.readouterr().out


def test_altered_metadata_blocks_duplicate_attempt(tmp_path: Path, monkeypatch):
    video, metadata = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "load_credentials", Mock(return_value=object()))
    monkeypatch.setattr(cli, "build_youtube_client", Mock(return_value=object()))

    def interrupted(youtube, request, **kwargs):
        kwargs["on_started"]()
        raise cli.YouTubeError("DUMMY_FAIL_CLOSED")

    upload_mock = Mock(side_effect=interrupted)
    monkeypatch.setattr(cli, "upload_video", upload_mock)
    assert cli.main(argv(video, metadata, "--yes")) == 2
    assert cli.main(argv(video, metadata, "--yes", "--title", "New title")) == 2
    assert upload_mock.call_count == 1


def test_bad_metadata_fails_before_google_or_sidecar(tmp_path: Path, monkeypatch, capsys):
    video, metadata = setup(tmp_path, monkeypatch, privacy="invalid")
    no_network = Mock(side_effect=AssertionError("No calls before valid metadata"))
    monkeypatch.setattr(cli, "load_credentials", no_network)
    monkeypatch.setattr(cli, "build_youtube_client", no_network)
    assert cli.main(argv(video, metadata, "--yes")) == 2
    assert "privacy" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.youtube-upload-attempt.json"))
    no_network.assert_not_called()


def test_manual_confirmation_required_before_any_network(tmp_path: Path, monkeypatch):
    video, metadata = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    no_network = Mock(side_effect=AssertionError("No network before confirmation"))
    monkeypatch.setattr(cli, "load_credentials", no_network)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv(video, metadata))
    assert exc.value.code == 2
    no_network.assert_not_called()
