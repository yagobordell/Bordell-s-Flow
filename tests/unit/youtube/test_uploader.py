"""Offline session, retry, error redaction and request-body tests."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_video_factory.integrations.youtube.errors import (
    YouTubeAmbiguousUploadError,
    YouTubeAuthenticationError,
    YouTubeUploadError,
)
from ai_video_factory.integrations.youtube.models import YouTubePublishRequest
from ai_video_factory.integrations.youtube.uploader import CHUNK_SIZE, upload_video


class FakeHttpError(Exception):
    def __init__(self, code: int, reason: str, retry_after: str | None = None):
        super().__init__("DUMMY_SHOULD_NEVER_APPEAR refresh_token=DUMMY_SECRET")
        self.resp = {
            "retry-after": retry_after,
        }
        self.resp = SimpleNamespace(status=code, get=self.resp.get)
        self.content = json.dumps({
            "error": {"errors": [{"reason": reason, "message": "DUMMY_SECRET"}]}
        }).encode("utf-8")


def request(tmp_path: Path, **overrides) -> YouTubePublishRequest:
    args = {
        "video_path": tmp_path / "clip.mp4",
        "channel": "test01",
        "title": "Integration test",
        "keywords": ["two words"],
        "category_id": "22",
        "privacy": "private",
    }
    args.update(overrides)
    return YouTubePublishRequest.model_validate(args)


def fake_youtube(sequence):
    operation = Mock()
    operation.next_chunk.side_effect = sequence
    video_api = Mock()
    video_api.insert.return_value = operation
    youtube = Mock()
    youtube.videos.return_value = video_api
    return youtube, video_api, operation


def test_successful_resumable_upload_has_correct_parts_and_single_session(tmp_path: Path):
    publish = datetime(2040, 1, 1, tzinfo=UTC)
    req = request(
        tmp_path,
        publish_at=publish,
        localizations={"en": {"title": "English"}},
        default_language="es",
        recording_date=datetime(2020, 1, 1, tzinfo=UTC),
    )
    youtube, api, operation = fake_youtube([
        (SimpleNamespace(progress=lambda: 0.5), None),
        (SimpleNamespace(progress=lambda: 0.8), None),
        (None, {"id": "testVideo_001"}),
    ])
    media = Mock(return_value=object())
    progress = []
    callbacks = []
    result = upload_video(
        youtube, req, media_factory=media, progress=progress.append,
        on_started=lambda: callbacks.append("reserved"),
        on_completed=lambda vid: callbacks.append(("complete", vid)),
    )
    media.assert_called_once_with(
        filename=str(req.video_path), mimetype="video/mp4",
        chunksize=CHUNK_SIZE, resumable=True,
    )
    kw = api.insert.call_args.kwargs
    assert kw["part"] == "snippet,status,localizations,recordingDetails"
    assert kw["notifySubscribers"] is False
    assert kw["body"]["snippet"]["tags"] == ["two words"]
    assert kw["body"]["status"]["publishAt"] == publish.isoformat()
    assert operation.next_chunk.call_count == 3
    operation.next_chunk.assert_called_with(num_retries=0)
    assert progress == [50, 80, 100]
    assert callbacks == ["reserved", ("complete", "testVideo_001")]
    assert result.url == "https://www.youtube.com/watch?v=testVideo_001"
    assert result.thumbnail_uploaded is False


@pytest.mark.parametrize("error", [
    FakeHttpError(503, "backendError"),
    FakeHttpError(429, "rateLimitExceeded", "3"),
    TimeoutError("DUMMY_SECRET"),
    ConnectionError("DUMMY_SECRET"),
])
def test_transient_errors_retry_in_same_session_without_leaking(tmp_path: Path, error):
    youtube, _, operation = fake_youtube([error, (None, {"id": "video_001"})])
    sleeps = []
    result = upload_video(
        youtube, request(tmp_path), media_factory=Mock(return_value=object()),
        sleep=sleeps.append, jitter=lambda: 0.25,
    )
    assert result.video_id == "video_001"
    assert operation.next_chunk.call_count == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 1.25
    if getattr(error, "resp", None) and error.resp.status == 429:
        assert sleeps[0] >= 3


@pytest.mark.parametrize("error", [
    FakeHttpError(403, "quotaExceeded"),
    FakeHttpError(400, "uploadLimitExceeded"),
    FakeHttpError(400, "invalidTags"),
])
def test_permanent_errors_have_no_auto_retries_and_no_provider_body(
    tmp_path: Path, error
):
    youtube, _, operation = fake_youtube([error])
    sleeps = []
    with pytest.raises(YouTubeUploadError) as captured:
        upload_video(
            youtube, request(tmp_path), media_factory=Mock(return_value=object()),
            sleep=sleeps.append,
        )
    assert operation.next_chunk.call_count == 1
    assert sleeps == []
    assert "DUMMY_SECRET" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_unauthorized_token_does_not_reauthorize_or_retry(tmp_path: Path):
    youtube, _, operation = fake_youtube([FakeHttpError(401, "authError")])
    with pytest.raises(YouTubeAuthenticationError) as captured:
        upload_video(youtube, request(tmp_path), media_factory=Mock(return_value=object()))
    assert "DUMMY_SECRET" not in str(captured.value)
    assert operation.next_chunk.call_count == 1


def test_five_consecutive_failures_are_bounded_and_fail_closed(tmp_path: Path):
    youtube, _, operation = fake_youtube(
        [FakeHttpError(503, "backendError")] * 5
    )
    sleeps = []
    with pytest.raises(YouTubeAmbiguousUploadError):
        upload_video(
            youtube, request(tmp_path), media_factory=Mock(return_value=object()),
            sleep=sleeps.append, jitter=lambda: 0,
        )
    assert operation.next_chunk.call_count == 5
    assert sleeps == [1, 2, 4, 8]


def test_progress_resets_consecutive_errors(tmp_path: Path):
    seq = [
        TimeoutError("secret"),
        (SimpleNamespace(progress=lambda: 0.1), None),
        TimeoutError("secret"),
        (SimpleNamespace(progress=lambda: 0.5), None),
        (None, {"id": "ok123"}),
    ]
    youtube, _, operation = fake_youtube(seq)
    sleeps = []
    result = upload_video(
        youtube, request(tmp_path), media_factory=Mock(return_value=object()),
        sleep=sleeps.append, jitter=lambda: 0,
    )
    assert result.video_id == "ok123"
    assert sleeps == [1, 1]
    assert operation.next_chunk.call_count == 5


@pytest.mark.parametrize("response", [{}, {"id": ""}, {"id": "unsafe?value"}])
def test_missing_or_unsafe_id_is_ambiguous(tmp_path: Path, response):
    youtube, _, _ = fake_youtube([(None, response)])
    with pytest.raises(YouTubeAmbiguousUploadError):
        upload_video(youtube, request(tmp_path), media_factory=Mock(return_value=object()))


def test_no_network_is_attempted_if_insert_construction_fails(tmp_path: Path):
    youtube = Mock()
    youtube.videos.return_value.insert.side_effect = RuntimeError("DUMMY_SECRET")
    on_started = Mock()
    with pytest.raises(YouTubeUploadError) as captured:
        upload_video(
            youtube, request(tmp_path), media_factory=Mock(return_value=object()),
            on_started=on_started,
        )
    assert "DUMMY_SECRET" not in str(captured.value)
    on_started.assert_not_called()


def test_non_network_unknown_error_is_ambiguous(tmp_path: Path):
    youtube, _, operation = fake_youtube([RuntimeError("DUMMY_SECRET")])
    with pytest.raises(YouTubeAmbiguousUploadError) as captured:
        upload_video(youtube, request(tmp_path), media_factory=Mock(return_value=object()))
    assert operation.next_chunk.call_count == 1
    assert "DUMMY_SECRET" not in str(captured.value)


def test_status_progress_on_huge_video_does_not_stall_on_integer_percent(tmp_path: Path):
    seq = [
        (SimpleNamespace(progress=lambda index=i: index / 10000), None)
        for i in range(1, 25)
    ] + [(None, {"id": "ok123"})]
    youtube, _, _ = fake_youtube(seq)
    result = upload_video(
        youtube, request(tmp_path), media_factory=Mock(return_value=object())
    )
    assert result.video_id == "ok123"
