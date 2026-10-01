"""Versioned, strict publishing contracts independent of the YouTube API client."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import YouTubeConfigurationError
from .store import validate_channel_name

Privacy = Literal["private", "unlisted", "public"]
License = Literal["youtube", "creativeCommon"]


def _validate_title(value: str) -> str:
    if not value.strip() or "<" in value or ">" in value or len(value) > 100:
        raise ValueError("YouTube title must be 1-100 characters without angle brackets")
    return value


def _validate_description(value: str) -> str:
    if "<" in value or ">" in value or len(value.encode("utf-8")) > 5000:
        raise ValueError("YouTube description exceeds limits or contains angle brackets")
    return value


class YouTubeLocalization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _validate_title(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_description(value)


class YouTubePublishRequest(BaseModel):
    """Metadata contract; file/probe validation happens before videos.insert in phase 3."""

    model_config = ConfigDict(extra="forbid")

    video_path: Path
    channel: str
    title: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    category_id: str = "22"
    privacy: Privacy = "private"
    publish_at: datetime | None = None
    notify_subscribers: bool = False
    made_for_kids: bool = False
    contains_synthetic_media: bool | None = True
    embeddable: bool = True
    public_stats_viewable: bool = True
    license: License = "youtube"
    default_language: str | None = None
    localizations: dict[str, YouTubeLocalization] = Field(default_factory=dict)
    recording_date: datetime | None = None
    thumbnail_path: Path | None = None

    @field_validator("channel")
    @classmethod
    def check_channel(cls, value: str) -> str:
        try:
            return validate_channel_name(value)
        except YouTubeConfigurationError as exc:
            raise ValueError("Invalid YouTube channel") from exc

    @field_validator("title")
    @classmethod
    def check_title(cls, value: str) -> str:
        return _validate_title(value)

    @field_validator("description")
    @classmethod
    def check_description(cls, value: str) -> str:
        return _validate_description(value)

    @field_validator("keywords")
    @classmethod
    def check_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        clean = tuple(word.strip() for word in value)
        if any(not word for word in clean):
            raise ValueError("YouTube tags may not be empty")
        serialized = ",".join(f'"{word}"' if " " in word else word for word in clean)
        if len(serialized) > 500:
            raise ValueError("Combined YouTube tags must be at most 500 characters")
        return clean

    @field_validator("category_id")
    @classmethod
    def check_category(cls, value: str) -> str:
        if not value.isascii() or not value.isdecimal():
            raise ValueError("YouTube category ID must contain digits")
        return value

    @field_validator("default_language")
    @classmethod
    def check_language(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("YouTube default language must not be blank")
        return value

    @model_validator(mode="after")
    def check_schedule_and_localizations(self) -> YouTubePublishRequest:
        if self.publish_at is not None:
            if self.privacy != "private":
                raise ValueError("Scheduled YouTube videos must use private privacy")
            if self.publish_at.tzinfo is None or self.publish_at.utcoffset() is None:
                raise ValueError("YouTube publish_at must include a timezone")
            if self.publish_at <= datetime.now(UTC):
                raise ValueError("YouTube publish_at must be in the future")
        if self.recording_date is not None and (
            self.recording_date.tzinfo is None or self.recording_date.utcoffset() is None
        ):
            raise ValueError("YouTube recording_date must include a timezone")
        if self.localizations and not self.default_language:
            raise ValueError("YouTube localizations require default_language")
        return self


class YouTubePublishResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    privacy: Privacy
    channel: str
    thumbnail_uploaded: bool
