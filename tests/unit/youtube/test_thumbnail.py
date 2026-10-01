"""Offline thumbnail validation, independent retries and Google request isolation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from ai_video_factory.integrations.youtube.errors import (
    YouTubeThumbnailError,
    YouTubeValidationError,
)
from ai_video_factory.integrations.youtube.thumbnail import (
    MAX_THUMBNAIL_BYTES,
    set_thumbnail,
    validate_thumbnail,
)


def image_file(tmp_path: Path, suffix: str = ".png") -> Path:
    path = tmp_path / ("thumb" + suffix)
    image = Image.new("RGB", (64, 36))
    image.save(path, format="PNG" if suffix == ".png" else "JPEG")
    return path


class FakeHttpError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__("Never echo refresh_token=DUMMY_SECRET")
        self.resp = SimpleNamespace(status=code, get=lambda key: None)
        self.content = json.dumps({
            "error": {"errors": [{"reason": reason, "message": "DUMMY_SECRET"}]}
        }).encode("utf-8")


def youtube_client(sequence):
    request = Mock()
    request.execute.side_effect = sequence
    api = Mock()
    api.set.return_value = request
    youtube = Mock()
    youtube.thumbnails.return_value = api
    return youtube, api, request.execute


@pytest.mark.parametrize(("suffix", "mimetype"), [
    (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
])
def test_verified_images_and_correct_media_type(
    tmp_path: Path, suffix: str, mimetype: str
) -> None:
    path = image_file(tmp_path, suffix)
    assert validate_thumbnail(path) == (path, mimetype, path.stat().st_size)
    youtube, api, execute = youtube_client([{"items": [{}]}])
    media_factory = Mock(return_value=object())
    set_thumbnail(youtube, "video_123", path, media_factory=media_factory)
    media_factory.assert_called_once_with(
        filename=str(path), mimetype=mimetype, resumable=False,
    )
    assert api.set.call_args.kwargs["videoId"] == "video_123"
    execute.assert_called_once_with(num_retries=0)
    assert youtube.videos.call_count == 0


@pytest.mark.parametrize("bad", ["missing", "mismatch", "garbage", "empty", "other"])
def test_bad_images_fail_before_google_request(tmp_path: Path, bad: str) -> None:
    target = tmp_path / "thumb.png"
    if bad == "mismatch":
        target = image_file(tmp_path, ".jpg")
        target.rename(tmp_path / "thumb.png")
        target = tmp_path / "thumb.png"
    elif bad == "garbage":
        target.write_bytes(b"\x89PNG invalid")
    elif bad == "empty":
        target.touch()
    elif bad == "other":
        target = tmp_path / "thumb.webp"
        target.write_bytes(b"not an image")
    client = Mock()
    with pytest.raises(YouTubeValidationError):
        set_thumbnail(client, "video_123", target, media_factory=Mock())
    client.assert_not_called()


def test_real_oversized_thumbnail_is_rejected(tmp_path: Path) -> None:
    target = image_file(tmp_path)
    with target.open("r+b") as stream:
        stream.truncate(MAX_THUMBNAIL_BYTES + 1)
    with pytest.raises(YouTubeValidationError):
        validate_thumbnail(target)


def test_symlink_and_invalid_id_rejected(tmp_path: Path) -> None:
    path = image_file(tmp_path)
    with pytest.raises(YouTubeValidationError):
        set_thumbnail(Mock(), "unsafe/id", path, media_factory=Mock())
    link = tmp_path / "link.png"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(YouTubeValidationError):
        validate_thumbnail(link)


@pytest.mark.parametrize("error", [
    FakeHttpError(503, "backendError"),
    FakeHttpError(429, "rateLimitExceeded"),
    TimeoutError("DUMMY_SECRET"),
])
def test_transient_errors_retry_thumbnail_only(
    tmp_path: Path, error: Exception
) -> None:
    youtube, api, execute = youtube_client([error, {"items": [{}]}])
    delays: list[float] = []
    set_thumbnail(
        youtube, "video_123", image_file(tmp_path),
        media_factory=Mock(return_value=object()),
        sleep=delays.append, jitter=lambda: 0.0,
    )
    assert execute.call_count == 2
    assert len(delays) == 1
    assert youtube.videos.call_count == 0
    assert api.set.call_count == 2


@pytest.mark.parametrize("error", [
    FakeHttpError(403, "forbidden"),
    FakeHttpError(403, "quotaExceeded"),
    FakeHttpError(400, "invalidImage"),
])
def test_permanent_error_no_retry_and_no_secret(
    tmp_path: Path, error: Exception
) -> None:
    youtube, _, execute = youtube_client([error])
    delays = []
    with pytest.raises(YouTubeThumbnailError) as caught:
        set_thumbnail(
            youtube, "video_123", image_file(tmp_path),
            media_factory=Mock(return_value=object()),
            sleep=delays.append,
        )
    assert "DUMMY_SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert delays == []
    execute.assert_called_once()
    assert youtube.videos.call_count == 0


def test_five_retry_failures_have_bounded_backoff(tmp_path: Path) -> None:
    youtube, _, execute = youtube_client([TimeoutError("DUMMY_SECRET")] * 5)
    delays = []
    with pytest.raises(YouTubeThumbnailError):
        set_thumbnail(
            youtube, "video_123", image_file(tmp_path),
            media_factory=Mock(return_value=object()),
            sleep=delays.append, jitter=lambda: 0.0,
        )
    assert delays == [1, 2, 4, 8]
    assert execute.call_count == 5


def test_uncertain_response_is_not_considered_success(tmp_path: Path) -> None:
    youtube, _, _ = youtube_client([{}])
    with pytest.raises(YouTubeThumbnailError):
        set_thumbnail(
            youtube, "video_123", image_file(tmp_path),
            media_factory=Mock(return_value=object()),
        )
