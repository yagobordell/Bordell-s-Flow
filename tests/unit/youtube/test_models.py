"""Strict publication metadata without any external YouTube calls."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_video_factory.integrations.youtube.models import (
    YouTubeLocalization,
    YouTubePublishRequest,
    YouTubePublishResult,
)


def request(**overrides: object) -> YouTubePublishRequest:
    values = {"video_path": "final.mp4", "channel": "Canal", "title": "Título"}
    values.update(overrides)
    return YouTubePublishRequest.model_validate(values)


def test_minimal_request_and_defaults() -> None:
    item = request()
    assert item.privacy == "private"
    assert item.category_id == "22"
    assert item.contains_synthetic_media is True
    assert item.localizations == {}


@pytest.mark.parametrize("title", ["", " ", "<bad>", ">" * 3, "a" * 101])
def test_title_constraints(title: str) -> None:
    with pytest.raises(ValidationError):
        request(title=title)


def test_description_utf8_limit() -> None:
    assert len(request(description="é" * 2500).description.encode("utf-8")) == 5000
    for description in ("é" * 2501, "unsafe <html>"):
        with pytest.raises(ValidationError):
            request(description=description)


def test_keyword_quoted_spaces_and_comma_count() -> None:
    assert request(keywords=["two words", "other"]).keywords == ("two words", "other")
    assert request(keywords=["a" * 500]).keywords == ("a" * 500,)
    for keywords in (["two words", "a" * 489], ["", "valid"]):
        with pytest.raises(ValidationError):
            request(keywords=keywords)


def test_schedule_requires_private_future_timezone() -> None:
    future = datetime.now(UTC) + timedelta(days=2)
    assert request(publish_at=future).publish_at == future
    for changes in (
        {"publish_at": future, "privacy": "public"},
        {"publish_at": future.replace(tzinfo=None)},
        {"publish_at": datetime.now(UTC) - timedelta(days=1)},
    ):
        with pytest.raises(ValidationError):
            request(**changes)


def test_localizations_require_default_language() -> None:
    localizations = {"en": YouTubeLocalization(title="An English title")}
    with pytest.raises(ValidationError):
        request(localizations=localizations)
    assert request(localizations=localizations, default_language="es").localizations == (
        localizations
    )


def test_recording_date_needs_timezone() -> None:
    with pytest.raises(ValidationError):
        request(recording_date=datetime(2026, 9, 1))
    assert request(recording_date=datetime(2026, 9, 1, tzinfo=UTC))


@pytest.mark.parametrize("changes", [
    {"category_id": "history"},
    {"privacy": "friends"},
    {"license": "custom"},
    {"channel": "../escape"},
    {"default_language": ""},
    {"unknown": True},
])
def test_invalid_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


def test_independent_default_localizations() -> None:
    first = request()
    second = request()
    first.localizations["en"] = YouTubeLocalization(title="One")
    assert second.localizations == {}


def test_result_contract() -> None:
    item = YouTubePublishResult(
        video_id="ID", url="https://youtube.com/watch?v=ID", privacy="private",
        channel="Canal", thumbnail_uploaded=False,
    )
    assert item.video_id == "ID"
