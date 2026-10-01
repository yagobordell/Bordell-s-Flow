"""Real Google auth library compatibility without making any network calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

google_credentials = pytest.importorskip("google.oauth2.credentials")

from ai_video_factory.integrations.youtube import auth
from ai_video_factory.integrations.youtube.store import YouTubeStore

SCOPE = auth.YOUTUBE_UPLOAD_SCOPE


def _info() -> dict[str, object]:
    return {
        "client_id": "DUMMY_GOOGLE_CLIENT_ID_FOR_TESTS",
        "client_secret": "DUMMY_GOOGLE_CLIENT_SECRET_FOR_TESTS",
        "refresh_token": "DUMMY_GOOGLE_REFRESH_TOKEN_FOR_TESTS",
        "token_uri": "https://oauth2.googleapis.com/token",
        "token": "DUMMY_GOOGLE_ACCESS_TOKEN_FOR_TESTS",
        "scopes": [SCOPE],
        "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def test_real_google_credentials_roundtrip_without_network(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "external")
    store.save_token("Canal", _info())
    with patch.object(
        google_credentials.Credentials, "refresh", side_effect=AssertionError("network")
    ):
        credentials = auth.load_credentials("Canal", store)
    assert isinstance(credentials, google_credentials.Credentials)
    assert credentials.valid
    assert credentials.refresh_token == "DUMMY_GOOGLE_REFRESH_TOKEN_FOR_TESTS"


def test_real_google_credentials_refresh_is_persisted_without_network(tmp_path: Path) -> None:
    store = YouTubeStore(tmp_path / "external")
    info = _info()
    info["expiry"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    store.save_token("Canal", info)

    def fake_refresh(credentials, request):
        credentials.token = "DUMMY_NEW_ACCESS_TOKEN_FOR_TESTS"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    with patch.object(google_credentials.Credentials, "refresh", fake_refresh):
        credentials = auth.load_credentials("Canal", store, request_factory=lambda: object())
    assert credentials.valid
    saved = json.loads(store.token_path("Canal").read_text(encoding="utf-8"))
    assert saved["token"] == "DUMMY_NEW_ACCESS_TOKEN_FOR_TESTS"
    assert saved["refresh_token"] == "DUMMY_GOOGLE_REFRESH_TOKEN_FOR_TESTS"
