"""Small phase-3 persistence guard; never authorize a duplicate videos.insert."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_video_factory.integrations.youtube.attempt import UploadAttempt
from ai_video_factory.integrations.youtube.errors import (
    YouTubeAmbiguousUploadError,
    YouTubeValidationError,
)
from ai_video_factory.integrations.youtube.models import YouTubePublishRequest
from ai_video_factory.integrations.youtube.uploader import upload_video


def make_request(tmp_path: Path, **changes) -> YouTubePublishRequest:
    file = tmp_path / "clip.mp4"
    if not file.exists():
        file.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    metadata = {"video_path": file, "channel": "test01", "title": "Test"}
    metadata.update(changes)
    return YouTubePublishRequest.model_validate(metadata)


def test_atomic_reservation_then_completion_and_exact_reuse(tmp_path: Path) -> None:
    req = make_request(tmp_path)
    first = UploadAttempt(req)
    first.fingerprint()
    assert first.existing_success() is None
    first.reserve()
    pending = json.loads(first.path.read_text())
    assert pending["status"] == "uploading"
    assert pending["video_id"] is None
    assert "refresh_token" not in first.path.read_text()
    first.complete("videoABC_123")

    again = UploadAttempt(req)
    again.fingerprint()
    previous = again.existing_success()
    assert previous is not None
    assert previous.video_id == "videoABC_123"


def test_ambiguous_previous_attempt_never_creates_new_reservation(tmp_path: Path) -> None:
    req = make_request(tmp_path)
    first = UploadAttempt(req)
    first.fingerprint()
    first.reserve()
    second = UploadAttempt(req)
    second.fingerprint()
    with pytest.raises(YouTubeAmbiguousUploadError):
        second.existing_success()
    with pytest.raises(YouTubeAmbiguousUploadError):
        second.reserve()
    assert first._read()["status"] == "uploading"


def test_changed_content_and_changed_metadata_fail_closed(tmp_path: Path) -> None:
    first_req = make_request(tmp_path)
    first = UploadAttempt(first_req)
    first.fingerprint()
    first.reserve()
    first.complete("original123")
    altered = UploadAttempt(make_request(tmp_path, title="new title"))
    altered.fingerprint()
    with pytest.raises(YouTubeAmbiguousUploadError):
        altered.existing_success()
    first_req.video_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40)
    edited_video = UploadAttempt(first_req)
    edited_video.fingerprint()
    with pytest.raises(YouTubeAmbiguousUploadError):
        edited_video.existing_success()


def test_changed_file_after_fingerprint_is_rejected_before_network(tmp_path: Path) -> None:
    attempt = UploadAttempt(make_request(tmp_path))
    attempt.fingerprint()
    attempt.request.video_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"X" * 80)
    with pytest.raises(YouTubeValidationError, match="changed"):
        attempt.reserve()
    assert not attempt.path.exists()


def test_completed_id_persistence_failure_keeps_ambiguous_guard(tmp_path: Path) -> None:
    attempt = UploadAttempt(make_request(tmp_path))
    attempt.fingerprint()
    attempt.reserve()
    with patch(
        "ai_video_factory.integrations.youtube.attempt.os.replace", side_effect=OSError()
    ):
        with pytest.raises(YouTubeAmbiguousUploadError, match="DO NOT re-upload"):
            attempt.complete("videoABC_123")
    saved = json.loads(attempt.path.read_text())
    assert saved["status"] == "uploading"


def test_real_uploader_callbacks_occupy_guard_until_confirmed(tmp_path: Path) -> None:
    from unittest.mock import Mock

    req = make_request(tmp_path)
    operation = Mock()
    operation.next_chunk.side_effect = [(None, {"id": "videoABC_123"})]
    youtube = Mock()
    youtube.videos.return_value.insert.return_value = operation
    attempt = UploadAttempt(req)
    attempt.fingerprint()

    result = upload_video(
        youtube, req, media_factory=Mock(return_value=object()),
        on_started=attempt.reserve,
        on_completed=attempt.complete,
    )
    assert result.video_id == "videoABC_123"
    assert json.loads(attempt.path.read_text())["video_id"] == result.video_id
    assert operation.next_chunk.call_count == 1

    retry = UploadAttempt(req)
    retry.fingerprint()
    assert retry.existing_success().video_id == result.video_id
    assert youtube.videos.return_value.insert.call_count == 1


def test_missing_or_malformed_attempt_record_fails_closed(tmp_path: Path) -> None:
    attempt = UploadAttempt(make_request(tmp_path))
    attempt.fingerprint()
    attempt.path.write_text("not json")
    with pytest.raises(YouTubeAmbiguousUploadError):
        attempt.existing_success()


def test_safe_error_diagnostics_preserve_ambiguous_attempt(tmp_path: Path) -> None:
    attempt = UploadAttempt(make_request(tmp_path))
    attempt.fingerprint()
    attempt.reserve()
    attempt.record_failure(403, "forbiddenPrivacySetting")
    record = json.loads(attempt.path.read_text())
    assert record["status"] == "ambiguous"
    assert record["last_http_status"] == 403
    assert record["last_api_reason"] == "forbiddenPrivacySetting"
    with pytest.raises(YouTubeAmbiguousUploadError):
        UploadAttempt(attempt.request).existing_success()
    assert "client_secret" not in attempt.path.read_text()
    assert "refresh_token" not in attempt.path.read_text()


def test_arbitrary_api_reason_is_never_persisted(tmp_path: Path) -> None:
    attempt = UploadAttempt(make_request(tmp_path))
    attempt.fingerprint()
    attempt.reserve()
    attempt.record_failure(403, "DUMMY_SECRET_CUSTOM_REASON")
    record = json.loads(attempt.path.read_text())
    assert record["last_http_status"] == 403
    assert record["last_api_reason"] is None
    assert "DUMMY_SECRET" not in attempt.path.read_text()
