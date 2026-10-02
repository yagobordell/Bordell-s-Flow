"""Verify deterministic per-channel locking and no sensitive lock file contents."""

from pathlib import Path

import pytest

from ai_video_factory.integrations.youtube.errors import YouTubeCredentialsError
from ai_video_factory.integrations.youtube.locking import channel_lock
from ai_video_factory.integrations.youtube.store import YouTubeStore


def test_lock_uses_no_channel_label_or_secrets_and_releases(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "external")
    store.ensure_directory()
    with channel_lock(store.directory, "Canal en Español"):
        locks = list(store.directory.glob(".oauth-*.lock"))
        assert len(locks) == 1
        assert "Canal" not in locks[0].name
        with pytest.raises(YouTubeCredentialsError, match="Another process"):
            with channel_lock(store.directory, "CANAL EN ESPAÑOL", timeout=0.01):
                pass
    # The same per-channel lock must become available again.
    with channel_lock(store.directory, "Canal en Español"):
        pass
    assert store.list_channels() == ()


def test_unicode_equivalent_channel_names_share_one_lock(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "external")
    store.ensure_directory()
    with channel_lock(store.directory, "Café"):
        with pytest.raises(YouTubeCredentialsError, match="Another process"):
            with channel_lock(store.directory, "Cafe\u0301", timeout=0.01):
                pass
    assert len(list(store.directory.glob(".oauth-*.lock"))) == 1
