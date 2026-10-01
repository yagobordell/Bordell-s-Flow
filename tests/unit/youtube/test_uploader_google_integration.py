"""Use real optional Google classes offline, without authenticating or uploading."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_video_factory.integrations.youtube.models import YouTubePublishRequest
from ai_video_factory.integrations.youtube.uploader import (
    CHUNK_SIZE,
    _safe_http_error,
    upload_video,
)

google_http = pytest.importorskip("googleapiclient.http")
google_errors = pytest.importorskip("googleapiclient.errors")
httplib2 = pytest.importorskip("httplib2")


def test_real_mediafileupload_has_exact_chunk_size(tmp_path: Path) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 20)
    request = YouTubePublishRequest(
        video_path=video, channel="test01", title="Sample",
    )
    api = Mock()
    operation = Mock()
    operation.next_chunk.return_value = (None, {"id": "videoABC_123"})
    api.insert.return_value = operation
    youtube = Mock()
    youtube.videos.return_value = api
    result = upload_video(youtube, request)
    assert result.video_id == "videoABC_123"
    media = api.insert.call_args.kwargs["media_body"]
    assert isinstance(media, google_http.MediaFileUpload)
    assert media.chunksize() == CHUNK_SIZE
    assert media.resumable() is True
    assert api.insert.call_args.kwargs["part"] == "snippet,status"


def test_real_google_http_error_parsing_is_allowlisted() -> None:
    fake_body = json.dumps({
        "error": {"errors": [{
            "reason": "quotaExceeded",
            "message": "DUMMY_NEVER_PRINT_TOKEN",
        }]}
    }).encode()
    response = httplib2.Response({"status": "403"})
    error = google_errors.HttpError(response, fake_body, uri="https://example.invalid")
    code, reason, retry_after = _safe_http_error(error)
    assert code == 403
    assert reason == "quotaExceeded"
    assert retry_after is None
    assert "DUMMY_NEVER_PRINT_TOKEN" not in str((code, reason, retry_after))


def test_unknown_provider_error_reason_does_not_escape() -> None:
    response = httplib2.Response({"status": "503"})
    error = google_errors.HttpError(
        response, json.dumps({
            "error": {"errors": [{
                "reason": "DUMMY_SECRET_NONSENSE", "message": "DUMMY_SECRET",
            }]}
        }).encode(), uri="https://example.invalid",
    )
    assert _safe_http_error(error) == (503, None, None)
