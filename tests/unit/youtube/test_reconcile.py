"""Explicit manual reconciliation is never a silent or automatic re-upload."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_video_factory.config import Settings
from ai_video_factory.integrations.youtube.errors import YouTubeValidationError
from ai_video_factory.integrations.youtube.models import (
    YouTubePublishRequest,
    YouTubePublishResult,
)
from ai_video_factory.integrations.youtube.publisher import publish_to_youtube
from ai_video_factory.integrations.youtube.state import (
    PublicationStateStore,
    metadata_digest,
    new_state,
    sha256_file,
)
from scripts.youtube import reconcile


def fixture(tmp_path: Path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
    request = YouTubePublishRequest(
        video_path=video, channel="test01", title="Ambiguous trial",
    )
    registry = PublicationStateStore(tmp_path / "registry")
    registry.ensure()
    state = new_state(
        request, sha256_file(video), metadata_digest(request), None,
    )
    state["status"] = "ambiguous"
    registry.write(state)
    monkeypatch.setattr(
        reconcile, "settings", Settings(
            _env_file=None,
            youtube_keystore_dir=tmp_path / "external-keystore",
        ),
    )
    base = [
        "--video", str(video), "--channel", "test01",
        "--state-dir", str(registry.root), "--acknowledge-studio-check", "--yes",
    ]
    return request, registry, base


def test_operator_found_existing_video_recovers_without_new_insert(
    tmp_path: Path, monkeypatch,
) -> None:
    req, registry, base = fixture(tmp_path, monkeypatch)
    assert reconcile.main([*base, "--found-video-id", "Existing_123"]) == 0
    state = registry.read(req.channel, sha256_file(req.video_path))
    assert state["status"] == "uploaded"
    assert state["video_id"] == "Existing_123"
    assert state["manual_reconciliation"] == "existing_video"
    duplicate = Mock(side_effect=AssertionError("Must not re-upload found video"))
    result = publish_to_youtube(
        req, keystore_dir=tmp_path / "external-keystore",
        state_dir=registry.root, youtube=object(), upload_fn=duplicate,
    )
    assert result.video_id == "Existing_123"
    duplicate.assert_not_called()


def test_operator_verified_no_video_requires_explicit_retry_flag(
    tmp_path: Path, monkeypatch,
) -> None:
    req, registry, base = fixture(tmp_path, monkeypatch)
    assert reconcile.main([*base, "--verified-not-created"]) == 0
    state = registry.read(req.channel, sha256_file(req.video_path))
    assert state["status"] == "failed_safe"
    assert state["manual_reconciliation"] == "verified_not_created"

    def fake_upload(youtube, request, **kwargs):
        kwargs["on_started"]()
        kwargs["on_completed"]("Reuploaded_123")
        return YouTubePublishResult(
            video_id="Reuploaded_123",
            url="https://www.youtube.com/watch?v=Reuploaded_123",
            privacy=request.privacy, channel=request.channel,
            thumbnail_uploaded=False,
        )

    upload = Mock(side_effect=fake_upload)
    args = {
        "keystore_dir": tmp_path / "external-keystore",
        "state_dir": registry.root, "youtube": object(), "upload_fn": upload,
    }
    with pytest.raises(YouTubeValidationError, match="retry-failed-safe"):
        publish_to_youtube(req, **args)
    upload.assert_not_called()
    result = publish_to_youtube(req, **args, retry_failed_safe=True)
    assert result.video_id == "Reuploaded_123"
    assert upload.call_count == 1


@pytest.mark.parametrize("flag", [
    "--verified-not-created", "--found-video-id",
])
def test_acknowledgement_is_mandatory(
    tmp_path: Path, monkeypatch, flag: str,
) -> None:
    req, registry, base = fixture(tmp_path, monkeypatch)
    arguments = [arg for arg in base if arg != "--acknowledge-studio-check"]
    arguments.extend(
        [flag, "some_ID"] if flag == "--found-video-id" else [flag]
    )
    with pytest.raises(SystemExit) as err:
        reconcile.main(arguments)
    assert err.value.code == 2
    assert registry.read(req.channel, sha256_file(req.video_path))["status"] == (
        "ambiguous"
    )


def test_invalid_video_id_is_rejected_without_state_changes(
    tmp_path: Path, monkeypatch,
) -> None:
    req, registry, base = fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as err:
        reconcile.main([*base, "--found-video-id", "bad/slash"])
    assert err.value.code == 2
    assert registry.read(req.channel, sha256_file(req.video_path))["status"] == (
        "ambiguous"
    )


def test_completed_state_cannot_be_reconciled_or_overwritten(
    tmp_path: Path, monkeypatch,
) -> None:
    req, registry, base = fixture(tmp_path, monkeypatch)
    current = registry.read(req.channel, sha256_file(req.video_path))
    current["status"] = "completed"
    current["video_id"] = "Already_live"
    current["url"] = "https://www.youtube.com/watch?v=Already_live"
    registry.write(current)
    with pytest.raises(SystemExit) as err:
        reconcile.main([*base, "--verified-not-created"])
    assert err.value.code == 2
    assert registry.read(req.channel, sha256_file(req.video_path))["video_id"] == (
        "Already_live"
    )
