"""YouTube configuration uses existing settings without demanding live credentials."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_video_factory.config import Settings


def test_youtube_is_disabled_without_auth_or_keystore() -> None:
    configured = Settings(_env_file=None, youtube_enabled=False, youtube_keystore_dir=None)
    assert configured.youtube_enabled is False
    assert configured.youtube_default_privacy == "private"
    assert configured.youtube_contains_synthetic_media is True


def test_settings_allow_optional_external_keystore_and_human_channel(tmp_path: Path) -> None:
    configured = Settings(
        _env_file=None, youtube_keystore_dir=tmp_path / "keystore",
        youtube_channel="  Canal Histórico ", youtube_default_language="es",
    )
    assert configured.youtube_channel == "Canal Histórico"
    assert configured.youtube_keystore_dir == tmp_path / "keystore"
    assert not configured.youtube_keystore_dir.exists()


def test_settings_reject_invalid_youtube_values() -> None:
    for override in (
        {"youtube_default_privacy": "friends"},
        {"youtube_license": "other"},
        {"youtube_default_category_id": "a"},
        {"youtube_channel": "../escape"},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **override)
