"""Cross-run video idempotency and independent thumbnail recovery, no Google calls."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from PIL import Image

from ai_video_factory.integrations.youtube.attempt import UploadAttempt
from ai_video_factory.integrations.youtube.errors import (
    YouTubeAmbiguousUploadError,
    YouTubeThumbnailError,
    YouTubeValidationError,
)
from ai_video_factory.integrations.youtube.models import (
    YouTubePublishRequest,
    YouTubePublishResult,
)
from ai_video_factory.integrations.youtube.publisher import (
    default_state_dir,
    publish_to_youtube,
)
from ai_video_factory.integrations.youtube.state import (
    PublicationStateStore,
    metadata_digest,
    new_state,
    sha256_file,
)


def request(tmp_path: Path, *, thumbnail: bool = False, title: str = "Safe") -> (
    YouTubePublishRequest
):
    video = tmp_path / "clip.mp4"
    if not video.exists():
        video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    image = None
    if thumbnail:
        image = tmp_path / "thumb.png"
        if not image.exists():
            Image.new("RGB", (128, 72)).save(image)
    return YouTubePublishRequest(
        video_path=video, channel="test01", title=title,
        thumbnail_path=image, privacy="private",
    )


def complete_upload(youtube, request, **kwargs):
    kwargs["on_started"]()
    kwargs["on_completed"]("Video_ID123")
    return YouTubePublishResult(
        video_id="Video_ID123",
        url="https://www.youtube.com/watch?v=Video_ID123",
        privacy=request.privacy,
        channel=request.channel,
        thumbnail_uploaded=False,
    )


def publish(req, tmp_path, **kwargs):
    return publish_to_youtube(
        req, keystore_dir=tmp_path / "keystore", state_dir=tmp_path / "registry",
        state_path=tmp_path / "youtube_publish.json", youtube=object(), **kwargs,
    )


def saved(req, tmp_path) -> dict:
    registry = PublicationStateStore(tmp_path / "registry")
    return registry.read(req.channel, sha256_file(req.video_path))


def test_first_upload_persists_video_id_before_thumbnail_then_reuses(tmp_path: Path):
    req = request(tmp_path, thumbnail=True)
    uploader = Mock(side_effect=complete_upload)
    snapshots = []

    def check_thumbnail(youtube, video_id, thumbnail_path):
        current = saved(req, tmp_path)
        mirror = json.loads((tmp_path / "youtube_publish.json").read_text())
        assert current["status"] == mirror["status"] == "uploaded"
        assert current["video_id"] == mirror["video_id"] == video_id
        snapshots.append(current)

    thumbnail = Mock(side_effect=check_thumbnail)
    first = publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    assert first.thumbnail_uploaded
    assert first.video_id == "Video_ID123"
    assert saved(req, tmp_path)["status"] == "completed"
    assert saved(req, tmp_path)["thumbnail_status"] == "uploaded"
    second = publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    assert second == first
    assert uploader.call_count == 1
    assert thumbnail.call_count == 1
    assert len(snapshots) == 1


def test_thumbnail_failure_only_retries_thumbnail(tmp_path: Path):
    req = request(tmp_path, thumbnail=True)
    uploader = Mock(side_effect=complete_upload)
    thumbnail = Mock(side_effect=[
        YouTubeThumbnailError("DUMMY first thumbnail failure"), None,
    ])
    with pytest.raises(YouTubeThumbnailError):
        publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    state = saved(req, tmp_path)
    assert state["status"] == "uploaded"
    assert state["thumbnail_status"] == "failed"
    assert state["video_id"] == "Video_ID123"
    result = publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    assert result.thumbnail_uploaded
    assert thumbnail.call_count == 2
    assert uploader.call_count == 1


def test_changed_thumbnail_updates_only_it(tmp_path: Path):
    req = request(tmp_path, thumbnail=True)
    uploader = Mock(side_effect=complete_upload)
    thumbnail = Mock()
    publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    Image.new("RGB", (90, 60)).save(req.thumbnail_path)
    publish(req, tmp_path, upload_fn=uploader, thumbnail_fn=thumbnail)
    assert uploader.call_count == 1
    assert thumbnail.call_count == 2
    assert saved(req, tmp_path)["thumbnail_sha256"] == sha256_file(req.thumbnail_path)


def test_same_video_different_filename_and_run_reuses_global_index(tmp_path: Path):
    req = request(tmp_path)
    uploader = Mock(side_effect=complete_upload)
    publish(req, tmp_path, upload_fn=uploader)
    second_path = tmp_path / "another_name.mp4"
    second_path.write_bytes(req.video_path.read_bytes())
    second = req.model_copy(update={"video_path": second_path})
    result = publish_to_youtube(
        second, keystore_dir=tmp_path / "keystore", state_dir=tmp_path / "registry",
        state_path=tmp_path / "second_run_state.json", youtube=object(),
        upload_fn=uploader,
    )
    assert result.video_id == "Video_ID123"
    assert uploader.call_count == 1


def test_changed_video_metadata_same_content_fails_closed(tmp_path: Path):
    original = request(tmp_path)
    uploader = Mock(side_effect=complete_upload)
    publish(original, tmp_path, upload_fn=uploader)
    changed = request(tmp_path, title="Different description")
    with pytest.raises(YouTubeValidationError, match="different metadata"):
        publish(changed, tmp_path, upload_fn=uploader)
    assert uploader.call_count == 1


def test_interrupted_upload_is_ambiguous_and_blocks_every_retry(tmp_path: Path):
    req = request(tmp_path)
    def crash(youtube, request, **kwargs):
        kwargs["on_started"]()
        raise YouTubeAmbiguousUploadError("network crash")
    uploader = Mock(side_effect=crash)
    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=uploader)
    assert saved(req, tmp_path)["status"] == "uploading"
    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=uploader)
    assert uploader.call_count == 1


def test_provider_error_is_recorded_safely_and_reuse_fails_closed(tmp_path: Path):
    req = request(tmp_path)
    def denied(youtube, request, **kwargs):
        kwargs["on_started"]()
        kwargs["on_failure"](403, "forbiddenPrivacySetting")
        raise YouTubeAmbiguousUploadError("provider rejected")
    uploader = Mock(side_effect=denied)
    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=uploader)
    state = saved(req, tmp_path)
    assert state["status"] == "ambiguous"
    assert state["last_http_status"] == 403
    assert state["last_api_reason"] == "forbiddenPrivacySetting"
    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=uploader)
    assert uploader.call_count == 1


def test_successful_phase3_sidecar_is_adopted_without_upload(tmp_path: Path):
    req = request(tmp_path)
    legacy = UploadAttempt(req)
    legacy.fingerprint()
    legacy.reserve()
    legacy.complete("Legacy_video_ID")
    uploader = Mock(side_effect=AssertionError("Must not reupload phase-3 success"))
    result = publish(req, tmp_path, upload_fn=uploader)
    assert result.video_id == "Legacy_video_ID"
    assert saved(req, tmp_path)["legacy_receipt_migrated"] is True
    assert saved(req, tmp_path)["status"] == "completed"
    uploader.assert_not_called()


def test_phase3_ambiguous_sidecar_blocks_initial_global_index(tmp_path: Path):
    req = request(tmp_path)
    legacy = UploadAttempt(req)
    legacy.fingerprint()
    legacy.reserve()
    uploader = Mock(side_effect=AssertionError("Must not send"))
    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=uploader)
    assert not (tmp_path / "registry").joinpath(
        PublicationStateStore.key(req.channel, sha256_file(req.video_path)) + ".json"
    ).exists()
    uploader.assert_not_called()


def test_explicit_only_failed_safe_retry(tmp_path: Path):
    req = request(tmp_path)
    state = new_state(req, sha256_file(req.video_path), metadata_digest(req), None)
    state["status"] = "failed_safe"
    store = PublicationStateStore(tmp_path / "registry")
    store.ensure()
    store.write(state)
    uploader = Mock(side_effect=complete_upload)
    with pytest.raises(YouTubeValidationError, match="retry-failed-safe"):
        publish(req, tmp_path, upload_fn=uploader)
    assert uploader.call_count == 0
    result = publish(req, tmp_path, upload_fn=uploader, retry_failed_safe=True)
    assert result.video_id == "Video_ID123"
    assert uploader.call_count == 1


def test_two_concurrent_process_like_calls_share_one_upload(tmp_path: Path):
    req = request(tmp_path)
    uploader = Mock(side_effect=complete_upload)
    started = Event()
    release = Event()

    def slow_upload(youtube, request, **kwargs):
        kwargs["on_started"]()
        started.set()
        assert release.wait(4.0)
        return uploader(youtube, request, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(
            publish, req, tmp_path, upload_fn=slow_upload,
        )
        assert started.wait(3.0)
        two = executor.submit(
            publish, req, tmp_path, upload_fn=slow_upload,
        )
        release.set()
        results = [one.result(timeout=8), two.result(timeout=8)]
    assert results[0].video_id == results[1].video_id
    assert uploader.call_count == 1


def test_no_thumbnail_marks_completed_without_google_if_reused(tmp_path: Path):
    req = request(tmp_path)
    fake = Mock(side_effect=complete_upload)
    result = publish(req, tmp_path, upload_fn=fake)
    assert result.thumbnail_uploaded is False
    result2 = publish(req, tmp_path, upload_fn=fake)
    assert result2 == result
    assert fake.call_count == 1


def test_state_root_defaults_to_external_sibling_and_override_must_be_absolute(
    tmp_path: Path,
) -> None:
    keystore = tmp_path / "keystores" / "youtube"
    assert default_state_dir(keystore) == keystore.parent / "youtube-publications"
    custom = tmp_path / "shared-publisher"
    assert default_state_dir(keystore, custom) == custom
    with pytest.raises(Exception, match="absolute"):
        default_state_dir(keystore, Path("relative-state"))
    with pytest.raises(Exception, match="outside"):
        default_state_dir(keystore, keystore)


def test_pending_preupload_metadata_can_be_corrected_safely(tmp_path: Path) -> None:
    initial = request(tmp_path)
    state = new_state(initial, sha256_file(initial.video_path), metadata_digest(initial), None)
    store = PublicationStateStore(tmp_path / "registry")
    store.ensure()
    store.write(state)
    updated = request(tmp_path, title="Fixed before first network request")
    uploader = Mock(side_effect=complete_upload)
    result = publish(updated, tmp_path, upload_fn=uploader)
    assert result.video_id == "Video_ID123"
    assert saved(updated, tmp_path)["metadata_sha256"] == metadata_digest(updated)
    assert uploader.call_count == 1


def test_unsafe_provider_diagnostic_is_not_serialized_to_registry(tmp_path: Path) -> None:
    req = request(tmp_path)

    def denied(youtube, request, **kwargs):
        kwargs["on_started"]()
        kwargs["on_failure"](999, "Bearer DUMMY_SECRET")
        raise YouTubeAmbiguousUploadError("unknown result")

    with pytest.raises(YouTubeAmbiguousUploadError):
        publish(req, tmp_path, upload_fn=denied)
    state = saved(req, tmp_path)
    assert state["status"] == "ambiguous"
    assert state["last_http_status"] is None
    assert state["last_api_reason"] is None
    assert "DUMMY_SECRET" not in (tmp_path / "youtube_publish.json").read_text()
