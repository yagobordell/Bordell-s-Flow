"""Strict, offline metadata validation and API body coverage for phase 3."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from ai_video_factory.config import Settings
from ai_video_factory.integrations.youtube.errors import YouTubeValidationError
from ai_video_factory.integrations.youtube.metadata import (
    MAX_VIDEO_BYTES,
    build_insert_payload,
    load_metadata,
    resolve_request,
    safe_summary,
    validate_video,
)


def video_file(tmp_path: Path, name: str = "test.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\0" * 128)
    return path


def metadata_file(tmp_path: Path, **changes) -> Path:
    metadata = {
        "schema_version": "youtube-publish/v1",
        "channel": "test01",
        "title": "A title",
    }
    metadata.update(changes)
    path = tmp_path / "youtube.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path


def config(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        youtube_default_privacy="unlisted",
        youtube_default_category_id="26",
        youtube_default_language="es",
        youtube_notify_subscribers=True,
        youtube_contains_synthetic_media=False,
    )


def test_precedence_cli_over_document_over_settings(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    metadata = metadata_file(
        tmp_path, title="JSON title", privacy="private",
        category_id="22", keywords=["old"],
    )
    result = resolve_request(
        video_path=video, metadata_path=metadata, config=config(tmp_path),
        overrides={"title": "CLI title", "keywords": ["new tag"], "category_id": "28"},
    )
    assert result.channel == "test01"
    assert result.privacy == "private"
    assert result.title == "CLI title"
    assert result.keywords == ("new tag",)
    assert result.category_id == "28"
    assert result.default_language == "es"
    assert result.contains_synthetic_media is False
    assert result.notify_subscribers is True


def test_no_title_in_defaults_requires_explicit_metadata_or_cli(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    document = metadata_file(tmp_path, title=None)
    with pytest.raises(YouTubeValidationError, match="title"):
        resolve_request(video_path=video, metadata_path=document, config=config(tmp_path))


def test_full_api_payload_matches_writable_youtube_insert_parts(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    future = datetime.now(UTC) + timedelta(days=10)
    document = metadata_file(
        tmp_path, description="Descripción", keywords=["two words", "test"],
        privacy="private", publish_at=future.isoformat(),
        made_for_kids=False, contains_synthetic_media=True,
        localizations={"en": {"title": "English", "description": "Description"}},
        recording_date=datetime(2020, 1, 2, tzinfo=UTC).isoformat(),
        notify_subscribers=False,
    )
    result = resolve_request(
        video_path=video, metadata_path=document, config=config(tmp_path)
    )
    parts, body = build_insert_payload(result)
    assert parts == "snippet,status,localizations,recordingDetails"
    assert body["snippet"] == {
        "title": "A title", "description": "Descripción", "categoryId": "26",
        "tags": ["two words", "test"], "defaultLanguage": "es",
    }
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["publishAt"] == future.isoformat()
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["embeddable"] is True
    assert body["status"]["license"] == "youtube"
    assert body["status"]["publicStatsViewable"] is True
    assert body["localizations"]["en"]["title"] == "English"
    assert "recordingDate" in body["recordingDetails"]
    summary = safe_summary(result, video.stat().st_size)
    assert summary["notify_subscribers"] is False
    assert summary["identity_verified"] is False
    assert "client_secret" not in str(summary)


def test_empty_tags_and_synthetic_none_are_omitted(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    document = metadata_file(tmp_path, keywords=[], contains_synthetic_media=None)
    request = resolve_request(
        video_path=video, metadata_path=document, config=config(tmp_path)
    )
    parts, body = build_insert_payload(request)
    assert parts == "snippet,status"
    assert "tags" not in body["snippet"]
    assert "containsSyntheticMedia" not in body["status"]


@pytest.mark.parametrize("bad", [
    {"unexpected": 123},
    {"channel": "../escape"},
    {"description": "e" * 5001},
    {"keywords": ["a" * 501]},
    {"privacy": "friends"},
    {"publish_at": "2020-01-01T00:00:00Z"},
    {"thumbnail_path": "thumbnail.png"},
])
def test_bad_metadata_is_rejected_before_api(tmp_path: Path, bad: dict) -> None:
    video = video_file(tmp_path)
    document = metadata_file(tmp_path, **bad)
    with pytest.raises(YouTubeValidationError):
        resolve_request(video_path=video, metadata_path=document, config=config(tmp_path))


def test_duplicate_json_keys_and_wrong_schema(tmp_path: Path) -> None:
    path = tmp_path / "youtube.json"
    path.write_text('{"schema_version":"youtube-publish/v1","title":"a","title":"b"}')
    with pytest.raises(YouTubeValidationError):
        load_metadata(path)
    path.write_text('{"schema_version":"youtube-publish/v999","title":"a"}')
    with pytest.raises(YouTubeValidationError):
        load_metadata(path)
    path.write_text('{"schema_version":"youtube-publish/v1","video_path":"wrong.mp4"}')
    with pytest.raises(YouTubeValidationError):
        load_metadata(path)


@pytest.mark.parametrize("data", [b"", b"not video", b"\0" * 300])
def test_fake_or_empty_mp4_rejected(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "fake.mp4"
    path.write_bytes(data)
    with pytest.raises(YouTubeValidationError):
        validate_video(path)


def test_wrong_extension_and_link_rejected(tmp_path: Path) -> None:
    video = video_file(tmp_path, "test.avi")
    with pytest.raises(YouTubeValidationError):
        validate_video(video)
    link = tmp_path / "linked.mp4"
    try:
        link.symlink_to(video)
    except (NotImplementedError, OSError):
        pytest.skip("Platform does not permit links")
    with pytest.raises(YouTubeValidationError):
        validate_video(link)


def test_video_limit_uses_decimal_gigabytes() -> None:
    assert MAX_VIDEO_BYTES == 256_000_000_000


def test_localizations_without_language_are_rejected(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    document = metadata_file(
        tmp_path, localizations={"en": {"title": "English"}}
    )
    with pytest.raises(YouTubeValidationError, match="default_language"):
        resolve_request(
            video_path=video, metadata_path=document, config=config(tmp_path),
            overrides={"default_language": None},
        )


def test_verified_video_factory_manifest_is_checked(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    marker = tmp_path / "manifest.json"
    marker.write_text(json.dumps({
        "schema_version": "managed-b-pipeline-v1",
        "files": {video.name: digest},
    }))
    assert validate_video(video)[0] == video
    video.write_bytes(video.read_bytes() + b"modification")
    with pytest.raises(YouTubeValidationError, match="manifest"):
        validate_video(video)


def test_manifest_missing_expected_video_hash_is_rejected(tmp_path: Path) -> None:
    video = video_file(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema_version": "managed-b-pipeline-v1",
        "files": {"unrelated.mp4": "a" * 64},
    }))
    with pytest.raises(YouTubeValidationError, match="manifest"):
        validate_video(video)


def test_relative_thumbnail_resolves_from_metadata_directory(
    tmp_path: Path,
) -> None:
    video = video_file(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    image = assets / "thumbnail.png"
    Image.new("RGB", (128, 72)).save(image)
    document = metadata_file(tmp_path, thumbnail_path="assets/thumbnail.png")
    result = resolve_request(
        video_path=video, metadata_path=document, config=config(tmp_path)
    )
    assert result.thumbnail_path == image
    assert safe_summary(result, video.stat().st_size)["thumbnail"] == "thumbnail.png"
    with pytest.raises(YouTubeValidationError):
        resolve_request(
            video_path=video, metadata_path=document, config=config(tmp_path),
            overrides={"thumbnail_path": str(tmp_path / "missing.png")},
        )
